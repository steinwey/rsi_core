import hashlib
import json
import os
import re
import tempfile
import time
from pathlib import Path


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def transient_file_access_error(exc):
    """Windows may temporarily deny replacement while a reader owns a handle."""
    return isinstance(exc, OSError) and getattr(exc, 'winerror', None) in (5, 32, 33)


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # A short unique name avoids both concurrent-writer collisions and adding
    # a long suffix to already deeply nested Windows evaluation paths.
    fd, name = tempfile.mkstemp(prefix='.rsi-', suffix='.tmp', dir=path.parent)
    temp = Path(name)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
        for attempt in range(6):
            try:
                temp.replace(path)
                return
            except OSError as exc:
                if not transient_file_access_error(exc) or attempt == 5:
                    raise
                time.sleep(0.05 * 2 ** attempt)
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass  # Do not mask the original I/O failure if cleanup is also blocked.


def safe_id(value):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", value) or value in (".", ".."):
        raise ValueError(f"Invalid task ID: {value!r}")
    return value


def file_map(directory):
    root = Path(directory)
    return {p.relative_to(root).as_posix(): p.read_text(encoding="utf-8")
            for p in sorted(root.rglob("*")) if p.is_file() and
            "__pycache__" not in p.parts and p.suffix in (".md", ".py")}


def digest(files):
    return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()[:16]


def parse_object(text):
    text = text.strip()
    if text.startswith("```"):
        text = "\n".join(text.splitlines()[1:-1])
    obj = json.loads(text)
    if not isinstance(obj, dict):
        raise ValueError("Expected a JSON object")
    return obj
