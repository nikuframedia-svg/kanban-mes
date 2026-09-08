"""Public identity of the code loaded by this process, captured at import."""
import hashlib
import subprocess
import sys
from pathlib import Path
from .config import settings


def code_fingerprint(root=None):
    root = root or Path(__file__).resolve().parents[1]
    digest = hashlib.sha256()
    paths = sorted(path for path in (root / "app").rglob("*")
                   if path.is_file() and "__pycache__" not in path.parts)
    for path in paths:
        digest.update(path.relative_to(root).as_posix().encode() + b"\0")
        digest.update(path.read_bytes().replace(b"\r\n", b"\n"))
        digest.update(b"\0")
    return digest.hexdigest()


def _commit():
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"],
                cwd=Path(__file__).resolve().parents[1], stderr=subprocess.DEVNULL,
                timeout=3, text=True).strip()
    except (OSError, subprocess.SubprocessError):
        return None


STARTUP_HEALTH = {"app": 'kanban-mes', "engine": settings.cross_engine,
                  "platform": sys.platform, "code_fingerprint": code_fingerprint(),
                  "commit": _commit()}
