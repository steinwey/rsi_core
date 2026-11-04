"""Version and validate the guide while keeping tools immutable."""
import os
import difflib
import hashlib
import re
import subprocess
import uuid
import shutil
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from rsi import ROOT
from rsi.core.common import digest, file_map, read_json, write_json
from rsi.skills.documents import CHECKS, validate_consistency
from rsi.skills.directory import INDEX_PATH, editable, immutable, read_skill_directory, validate_paths, reject_links
from rsi.skills.index import build_index, validate_index
from rsi.core.common import transient_file_access_error

SKILL_ROOT = "/opt/rsi-skills"
SCRIPT_PATH = re.compile(r"scripts/[A-Za-z][A-Za-z0-9_]{0,40}\.py")


class SkillProposalRejected(ValueError):
    """An invalid candidate leaves the current skill unchanged."""


def skill_files(skill):
    """Guide plus read-only tools available to Trace2Skill; exclude fixed checks."""
    return {k: v for k, v in file_map(skill).items() if editable(k) or SCRIPT_PATH.fullmatch(k)}


def load_initial_skill(source=None, *, directory_schema=0, bootstrap=False):
    """Read and validate a seed once; never change its source store or pointer."""
    builtin = ROOT / "skills/test-and-diagnose"
    fixed_files = {k: v for k, v in file_map(builtin).items() if not editable(k)}
    if source is not None and (not isinstance(source, (str, Path)) or not str(source).strip()):
        raise ValueError("initial_skill must be a nonempty path")
    raw_path = Path(source).expanduser().absolute() if source is not None else builtin.absolute()
    reject_links(raw_path)
    path = raw_path.resolve()
    requested = path
    pointer = None
    kind = "builtin" if source is None else "directory"
    if path.is_dir():
        if (path / "workflow.json").is_file() and (path / "train/skills/current.json").is_file():
            pointer, kind = path / "train/skills/current.json", "workflow"
        elif (path / "skills/current.json").is_file():
            pointer, kind = path / "skills/current.json", "run"
        elif (path / "current.json").is_file():
            pointer, kind = path / "current.json", "store"
    elif path.is_file() and path.name == "current.json":
        pointer, kind = path, "pointer"
    elif path.is_file() and path.name == "SKILL.md":
        kind = "document"
    else:
        raise ValueError(f"Initial skill not found or unsupported: {path}")
    if pointer is not None:
        try:
            version = read_json(pointer)["version"]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ValueError(f"Invalid skill pointer: {pointer}") from exc
        if not isinstance(version, str) or not re.fullmatch(r"[0-9a-f]{16}", version):
            raise ValueError(f"Invalid skill version in pointer: {pointer}")
        path = pointer.parent / "versions" / version
    elif kind == "directory" and re.fullmatch(r"[0-9a-f]{16}", path.name):
        kind = "version"
    if kind != "document" and not (path / "SKILL.md").is_file():
        raise ValueError(f"Initial skill directory must contain SKILL.md: {path}")
    files = ({**fixed_files, "SKILL.md": path.read_text(encoding="utf-8")}
             if kind == "document" else read_skill_directory(path, directory_schema=directory_schema))
    source_version = digest(files)
    if directory_schema and INDEX_PATH not in files:
        if not bootstrap:
            raise ValueError("Managed Skill requires an index")
        validate_document(files, fixed_files)
        files[INDEX_PATH] = build_index(files)
    validate_document(files, fixed_files, directory_schema=directory_schema)
    version = digest(files)
    if (pointer is not None or kind == "version") and (source_version if bootstrap else version) != path.name:
        raise ValueError(f"Initial skill content differs from its version: {path}")
    metadata = {"kind": kind, "requested_path": str(requested), "resolved_path": str(path.resolve()),
                "version": version, "tools_hash": digest(fixed_files),
                "document_sha256": hashlib.sha256(files["SKILL.md"].encode("utf-8")).hexdigest()}
    if directory_schema:
        metadata.update(directory_schema=directory_schema, source_version=source_version, derived_version=version)
    return files, metadata


class SkillStore:
    def __init__(self, root, seed, *, directory_schema=0, maintenance=None):
        from rsi.skills.maintenance import settings
        self.maintenance = settings({'skill_maintenance': maintenance}) if maintenance is not None else None
        if maintenance and maintenance.get('enabled') and directory_schema != 1:
            raise ValueError('Maintenance requires directory schema 1')
        self.directory_schema = directory_schema
        self.root = Path(root)
        reject_links(self.root)
        self.pointer = self.root / "current.json"
        files = dict(seed) if isinstance(seed, dict) else read_skill_directory(seed, directory_schema=directory_schema)
        self.fixed_files = immutable(files) if directory_schema else {k: v for k, v in files.items() if not editable(k)}
        self.tools_hash = digest(self.fixed_files)
        if not self.pointer.exists():
            version = self.materialize(files)
            write_json(self.pointer, {"version": version})

    def materialize(self, files):
        from rsi.skills.maintenance import validate_limits
        validate_limits(files, self.maintenance)
        validate_paths(files)
        if any(not isinstance(text, str) for text in files.values()):
            raise ValueError('Skill file contents must be text')
        if self.directory_schema:
            validate_document(files, self.fixed_files, directory_schema=self.directory_schema)
        version = digest(files)
        versions = self.root / 'versions'
        reject_links(versions)
        versions.mkdir(parents=True, exist_ok=True)
        folder = versions / version
        if folder.exists():
            self._verify(folder, files)
            return version
        stage = Path(tempfile.mkdtemp(prefix='.staging-', dir=versions))
        try:
            for name, text in sorted(files.items()):
                path = stage / name
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open('w', encoding='utf-8', newline='\n') as stream:
                    stream.write(text)
                    stream.flush()
                    os.fsync(stream.fileno())
            self._verify(stage, files)
            for attempt in range(6):
                try:
                    # Never replace an existing committed version.
                    stage.rename(folder)
                    break
                except OSError as exc:
                    if folder.exists():
                        self._verify(folder, files)
                        break
                    if not transient_file_access_error(exc) or attempt == 5:
                        raise
                    time.sleep(0.05 * 2 ** attempt)
            self._verify(folder, files)
            return version
        finally:
            if stage.exists():
                # Cleanup only this invocation's private staging directory.
                # A hard kill may leave an unreferenced staging directory, never current.
                shutil.rmtree(stage)

    def _verify(self, folder, expected):
        from rsi.skills.maintenance import validate_limits
        actual = read_skill_directory(folder, directory_schema=self.directory_schema)
        validate_limits(actual, self.maintenance)
        if actual != expected or digest(actual) != digest(expected):
            raise ValueError('Stored Skill version is incomplete or differs from candidate')
        if self.directory_schema:
            validate_document(actual, self.fixed_files, directory_schema=self.directory_schema)

    def current(self):
        version = read_json(self.pointer)['version']
        if not isinstance(version, str) or not re.fullmatch(r'[0-9a-f]{16}', version):
            raise ValueError('Invalid current Skill version')
        folder = self.root / 'versions' / version
        if self.directory_schema:
            files = read_skill_directory(folder, directory_schema=self.directory_schema)
            if digest(files) != version:
                raise ValueError('Current Skill version hash mismatch')
            validate_document(files, self.fixed_files, directory_schema=self.directory_schema)
            from rsi.skills.maintenance import validate_limits
            validate_limits(files, self.maintenance)
        return folder

    def propose(self, changes, *, managed_changes=None, expected_base_version=None):
        if not isinstance(changes, dict) or any(not editable(p) for p in changes):
            raise SkillProposalRejected("Only SKILL.md and references/*.md may change; tools and checks are fixed")
        if "SKILL.md" in changes and changes["SKILL.md"] is None:
            raise SkillProposalRejected("SKILL.md cannot be deleted")
        current = self.current()
        if expected_base_version is not None and current.name != expected_base_version:
            raise SkillProposalRejected('Stale Skill base version')
        old = read_skill_directory(current, directory_schema=self.directory_schema)
        new = {k: v for k, v in (old | changes).items() if v is not None}
        try:
            if any(not isinstance(text, str) for text in new.values()):
                raise ValueError('Skill file contents must be text')
            if managed_changes is not None:
                if not self.directory_schema or not isinstance(managed_changes, dict) or set(managed_changes) != {INDEX_PATH}:
                    raise ValueError('Managed changes may only contain routing/index.yaml in managed mode')
                # The independent interface accepts only deterministic program output.
                expected = build_index(new, validate_index(old), source_files=old if self.maintenance and self.maintenance.get('enabled') else None)
                if managed_changes[INDEX_PATH] != expected:
                    raise ValueError('Managed index must match deterministic builder output')
                new.update(managed_changes)
            elif self.directory_schema:
                new[INDEX_PATH] = build_index(new, validate_index(old), source_files=old if self.maintenance and self.maintenance.get('enabled') else None)
            from rsi.skills.maintenance import validate_limits
            validate_limits(new, self.maintenance)
            # An unchanged archived seed is not a newly accepted version.
            (validate_candidate if new != old else validate_document)(new, self.fixed_files, directory_schema=self.directory_schema)
        except ValueError as exc:
            raise SkillProposalRejected(str(exc)) from exc
        version = self.materialize(new)
        diff = "\n".join("\n".join(difflib.unified_diff(old.get(p, "").splitlines(), new.get(p, "").splitlines(),
                         fromfile=f"old/{p}", tofile=f"new/{p}", lineterm="")) for p in sorted(old.keys() | new.keys()) if old.get(p) != new.get(p))
        return self.root / "versions" / version, diff

    def record(self, candidate, accepted, evidence):
        if accepted:
            from rsi.skills.maintenance import validate_limits
            validate_limits(read_skill_directory(candidate, directory_schema=self.directory_schema), self.maintenance)
            validate_candidate(read_skill_directory(candidate, directory_schema=self.directory_schema), self.fixed_files, directory_schema=self.directory_schema)
        before = self.current().name
        event = {"timestamp": datetime.now(timezone.utc).isoformat(), "before": before,
                 "after": candidate.name, "accepted": accepted, **evidence, "tools_hash": self.tools_hash}
        # EvoMem-inspired append-only transition evidence, including rejected proposals.
        import json
        with (self.root / "history.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        if accepted:
            write_json(self.pointer, {"version": candidate.name})


def validate_document(files, fixed_files, *, directory_schema=0):
    validate_paths(files)
    if directory_schema:
        validate_index(files)
    fixed = immutable(files) if directory_schema else {k: v for k, v in files.items() if not editable(k)}
    if fixed != fixed_files:
        raise ValueError("Fixed tools or checks were modified")
    guide = files.get("SKILL.md")
    if not isinstance(guide, str) or not guide.strip():
        raise ValueError("SKILL.md must be non-empty text")
    for name, text in files.items():
        if editable(name) and (not isinstance(text, str) or not text.strip()):
            raise ValueError(f"Skill document must be nonempty: {name}")
    for ref in re.findall(r"scripts/[A-Za-z0-9_./-]+\.py", guide):
        if ref not in fixed_files:
            raise ValueError(f"Unknown fixed tool reference: {ref}")


def validate_candidate(files, fixed_files, *, directory_schema=0):
    validate_document(files, fixed_files, directory_schema=directory_schema)
    validate_consistency(files)


def document_gate(candidate, fixed_files, *, check_consistency=False, directory_schema=0, maintenance=None):
    # Archived seeds stay readable; the stronger gate is required for NEW versions.
    scope = 'document_consistency' if check_consistency else 'document_integrity'
    details = {'scope': scope, 'effectiveness_evaluated': False,
               'checks': ['fixed_files', 'nonempty_documents', 'fixed_tool_links'] + (CHECKS if check_consistency else [])}
    if directory_schema:
        details['checks'] += ['managed_index', 'directory_hash']
    try:
        files = read_skill_directory(candidate, directory_schema=directory_schema)
        from rsi.skills.maintenance import validate_limits
        validate_limits(files, maintenance)
        (validate_candidate if check_consistency else validate_document)(files, fixed_files, directory_schema=directory_schema)
        if directory_schema and digest(files) != Path(candidate).name:
            raise ValueError("Candidate directory hash mismatch")
        return {"passed": True, "tools_hash": digest(fixed_files), **details}
    except ValueError as exc:
        return {"passed": False, "error": str(exc), **details}


def functional_gate(candidate, fixed_tests, image="python:3.12-slim"):
    """Check the initial fixed tools in a restricted disposable container."""
    name = "rsi-gate-" + uuid.uuid4().hex[:12]
    command = ["docker", "run", "--rm", "--name", name, "--network", "none", "--read-only",
               "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
               "--pids-limit", "64", "--memory", "256m", "--cpus", "1",
               "--tmpfs", "/tmp:rw,nosuid,size=64m", "-e", "PYTHONDONTWRITEBYTECODE=1",
               "-v", f"{candidate.resolve()}:/skill:ro",
               "-v", f"{fixed_tests.resolve()}:/checks:ro", image,
               "python", "/checks/check_skill.py", "/skill"]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=900)
        return {"passed": result.returncode == 0, "stdout": result.stdout, "stderr": result.stderr}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"passed": False, "error": str(exc)}
    finally:
        # Killing a timed-out docker client does not necessarily stop its container.
        try:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            pass


