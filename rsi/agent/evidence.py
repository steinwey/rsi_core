"""Capture raw commands and build source-addressable trajectory evidence."""
import copy
import hashlib
import json
import re
import shlex
import subprocess
from pathlib import Path

from rsi.core.common import read_json, write_json
from rsi.core.model import object_messages


def script_invocations(command):
    """Recognize direct Python/script commands, not mentions or quoted code.

    These are invocation attempts in a submitted shell command. Shell branches
    may skip them; the recorded return code belongs to the whole command.
    """
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()<>")
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return []
    calls = set()
    start = True
    python = False
    for token in tokens:
        if token and all(c in ";&|()<>" for c in token):
            start, python = True, False
            continue
        if start:
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", token):
                continue
            start = False
            python = bool(re.fullmatch(r"(?:.*/)?python(?:[23](?:\.\d+)?)?", token))
            if python:
                continue
            if re.fullmatch(r"/opt/rsi-skills/scripts/[A-Za-z][A-Za-z0-9_]*\.py", token):
                calls.add(token.split('/opt/rsi-skills/', 1)[1])
        elif python:
            if token in ("-u", "-B", "-E", "-s", "-S", "-I"):
                continue
            if re.fullmatch(r"/opt/rsi-skills/scripts/[A-Za-z][A-Za-z0-9_]*\.py", token):
                calls.add(token.split('/opt/rsi-skills/', 1)[1])
            python = False
    return sorted(calls)


class RecordingEnvironment:
    def __init__(self, env, folder, message_index):
        self.env = env
        self.folder = Path(folder) / "commands"
        self.message_index = message_index
        self.count = 0
        self.script_calls = []

    def __getattr__(self, name):
        return getattr(self.env, name)

    @property
    def config(self):
        return self.env.config

    def get_template_vars(self):
        return self.env.get_template_vars()

    def _save(self, command, cwd, output, returncode=None, error=None, stderr=None):
        self.count += 1
        self.folder.mkdir(parents=True, exist_ok=True)
        record = {"sequence": self.count, "assistant_message": self.message_index(),
                  "command": command, "cwd": cwd, "returncode": returncode, "error": error}
        scripts = script_invocations(command)
        if scripts:
            self.script_calls.append({"message": record["assistant_message"], "sequence": self.count,
                                      "scripts": scripts, "returncode": returncode, "error": error})
        for key, value in (("output", output), ("stderr", stderr)):
            if value is None:
                continue
            raw = value if isinstance(value, bytes) else value.encode("utf-8")
            name = f"{self.count:06d}.{key}." + ("bin" if isinstance(value, bytes) else "txt")
            (self.folder / name).write_bytes(raw)
            record[key] = {"path": name, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
        write_json(self.folder / f"{self.count:06d}.json", record)
        with (self.folder / "index.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    def execute(self, command, cwd=""):
        try:
            output = self.env.execute(command, cwd=cwd)
        except Exception as exc:
            self._save(command, cwd, getattr(exc, "output", None),
                       error=f"{type(exc).__name__}: {exc}", stderr=getattr(exc, "stderr", None))
            # The upstream timeout path bypasses its normal 10,000-character
            # observation limit. Bound only the model-facing copy after archiving.
            if isinstance(exc, subprocess.TimeoutExpired) and exc.output:
                text = exc.output.decode("utf-8", errors="replace") if isinstance(exc.output, bytes) else exc.output
                if len(text) >= 10000:
                    exc.output = (text[:5000] + f"\n<elided_chars>{len(text) - 10000} characters elided</elided_chars>\n"
                                  + text[-5000:]).encode("utf-8")
            raise
        self._save(command, cwd, output.get("output", ""), output.get("returncode"))
        return output


VERSION = "steps-limit-v1"
NOTE = ("Included steps and the final artifact are complete verbatim text, with no character budget. "
        "A step is one assistant action plus the observations before the next action. At most "
        "{step_limit} steps are included; steps_omitted counts earlier steps left out. Message numbers "
        "still refer to the archived full trajectory. spans contain [start, end, text] with Python "
        "character offsets, and omitted is zero for included text. Tests describe the code at that "
        "step only. Initial context is shared by context_ref. Full source snapshots and raw command "
        "logs are archived on the host. Observations may already contain upstream output truncation.")


def serialized(value):
    return json.dumps(value, ensure_ascii=False)


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def public_rollout(result):
    # Never copy verifier scores or other backend metadata into model evidence.
    return copy.deepcopy({k: result[k] for k in
                          ("rollout", "status", "messages", "artifact", "error", "skill_injected",
                           "skill_version", "skill_script_called", "skill_script_calls", "tools_hash",
                           "action_protocol") if k in result})


def _render(text, ranges):
    return {"length": len(text), "omitted": len(text) - sum(b-a for a, b in ranges),
            "spans": [[a, b, text[a:b]] for a, b in ranges]}


def _complete(text, extra):
    return {**extra, **_render(text, [[0, len(text)]] if text else [])}


def build_evidence(results, step_limit, purpose="selector"):
    if purpose not in ("selector", "trace2skill"):
        raise ValueError("Unknown evidence purpose")
    if type(step_limit) is not int or step_limit <= 0:
        raise ValueError("step_limit must be a positive integer")
    shared, routes = {}, []
    for result in results:
        source = public_rollout(result)
        messages = source["messages"]
        first = next((i for i, m in enumerate(messages) if m["role"] == "assistant"), len(messages))
        prefix = messages[:first]
        context_id = sha(serialized(prefix))
        shared.setdefault(context_id, prefix)
        indexed = []
        step = -1
        for i, message in enumerate(messages[first:], first):
            if message["role"] == "assistant":
                step += 1
            indexed.append((i, step, message))
        total = step + 1 if step >= 0 else 0
        start = max(0, total - step_limit)
        evidence = [_complete(message.get("content", ""),
                              {"message": i, "step": step_no, "role": message["role"]})
                    for i, step_no, message in indexed if step_no >= start]
        artifact = source["artifact"]
        # Units start at each file/hunk; offsets identify them in the full patch.
        boundaries = sorted({0, len(artifact)} | {m.start() for m in
                            re.finditer(r"^(?:diff --git |@@)", artifact, re.M)})
        patch = [_complete(artifact[a:b], {"offset": a})
                 for a, b in zip(boundaries, boundaries[1:])]
        routes.append({"rollout": source["rollout"], "status": source["status"],
                       "action_protocol": source.get('action_protocol'),
                       "error": source["error"], "context_ref": context_id,
                       "source_sha256": sha(serialized(source)),
                       "step_limit": step_limit, "steps_total": total, "steps_omitted": start,
                       "messages": evidence, "artifact": patch})
    return {"evidence_format": VERSION, "evidence_note": NOTE.format(step_limit=step_limit),
            "shared_context": shared, "trajectories": routes}


def archive_request(folder, purpose, instruction, data, results, step_limit):
    """Write before provider invocation, including when that invocation later fails."""
    folder = Path(folder) / "evidence"
    sources = []
    for result in results:
        source = public_rollout(result)
        name = f'sources/rollout-{source["rollout"]}.json'
        write_json(folder / name, source)
        sources.append({"path": name, "sha256": sha(serialized(source)),
                        "trajectory": f'../rollout-{source["rollout"]}/trajectory.json',
                        "message_sha256": [sha(m.get("content", "")) for m in source["messages"]]})
        commands = Path(folder).parent / f'rollout-{source["rollout"]}/commands/index.jsonl'
        if commands.exists():
            sources[-1]["raw_commands"] = f'../rollout-{source["rollout"]}/commands/index.jsonl'
    request = object_messages(instruction, data)
    write_json(folder / f"{purpose}.json", request)
    path = folder / "manifest.json"
    manifest = read_json(path) if path.exists() else {"version": VERSION, "requests": {}}
    manifest["requests"][purpose] = {
        "request_sha256": sha(serialized(request)), "request_chars": len(serialized(request)),
        "representation": "model_request",
        "evidence_format": data.get("evidence_format", VERSION),
        "evidence_steps": step_limit, "sources": sources,
        "coverage": [{"rollout": r["rollout"],
            "summary_chars": len(r["summary"]) if "summary" in r else None,
            "summary_coverage": "unaudited" if "summary" in r else None,
            **{field: {
            "source_chars": sum(m["length"] for m in r[field]),
            "omitted_chars": sum(m["omitted"] for m in r[field]),
            "serialized_chars": len(serialized(r[field]))}
            for field in ("messages", "artifact") if field in r}} for r in data["trajectories"]]}
    write_json(path, manifest)


def judge_step_limit_note(limit):
    return (f"Each trajectory includes at most {limit} steps. steps_omitted counts earlier steps left out of "
            "this prompt; do not treat those missing steps as empty or successful.")
