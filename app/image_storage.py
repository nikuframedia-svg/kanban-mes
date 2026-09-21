"""Locate migrated originals by portable basename and verified content identity."""
from __future__ import annotations
import hashlib
import re
from functools import lru_cache
from pathlib import Path, PureWindowsPath
from .config import settings


@lru_cache(maxsize=2048)
def _digest(path, size, mtime, ctime):
    with Path(path).open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def digest(path):
    stat = path.stat()
    return _digest(str(path), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def expected_hash(sheet):
    value = str(sheet.get('image_sha256') or '').lower()
    return value if re.fullmatch(r'[0-9a-f]{64}', value) else None


def basename(sheet):
    # PureWindowsPath also understands '/' when running on Linux.
    name = PureWindowsPath(str(sheet.get('image_path') or '')).name
    if not name or name in {'.', '..'} or any(c in name for c in '<>:"/\\|?*'):
        return None
    return name


def destination(sheet):
    name = basename(sheet)
    return settings.images_dir / name if name else None


def resolve(sheet):
    """Never serve outside images_dir or substitute unverified relocated bytes."""
    stored = sheet.get('image_path')
    if not stored:
        return None
    root = settings.images_dir.resolve()
    expected = expected_hash(sheet)
    original = Path(stored).resolve()
    candidates = [(original, False)]
    target = destination(sheet)
    if target is not None and target.resolve() != original:
        candidates.append((target.resolve(), True))
    for path, relocated in candidates:
        try:
            if not path.is_relative_to(root) or not path.is_file():
                continue
            if relocated and not expected:
                continue
            if expected and digest(path) != expected:
                continue
            return path
        except OSError:
            continue
    return None


def for_processing(sheet):
    # Existing trusted local processing paths retain their previous behavior.
    # Migrated paths use the same confined, hash-verified resolution as /photo.
    path = Path(sheet['image_path']) if sheet.get('image_path') else None
    return path if path and path.is_file() else resolve(sheet)
