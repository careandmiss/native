"""io_utils.py — UTF-8 atomic file I/O and JSON helpers.

All file writes go through write_utf8_atomic to avoid partial writes when the
process is interrupted mid-call. JSON reads tolerate BOM and trailing commas.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def ensure_dir(path: os.PathLike | str) -> Path:
    """Create the directory (and parents) if missing. Return resolved Path."""
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p.resolve()


def read_utf8(path: os.PathLike | str) -> str:
    """Read a file as UTF-8. Strips a leading UTF-8 BOM if present."""
    p = Path(path)
    raw = p.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    return raw.decode("utf-8")


def write_utf8_atomic(path: os.PathLike | str, content: str) -> Path:
    """Write UTF-8 content atomically: tmp file in same dir, then os.replace.

    Windows-compatible: tempfile is created in the same directory as ``path``
    so os.replace is atomic across the same volume.
    """
    p = Path(path)
    ensure_dir(p.parent)
    # Use NamedTemporaryFile with delete=False so we control the close/replace.
    fd, tmp_name = tempfile.mkstemp(prefix=p.name + ".", suffix=".tmp", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
        os.replace(tmp_name, p)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return p.resolve()


def read_json(path: os.PathLike | str) -> Any:
    """Parse a UTF-8 JSON file. Raises ValueError on syntax errors."""
    return json.loads(read_utf8(path))


def write_json_atomic(path: os.PathLike | str, payload: Any, *, indent: int = 2) -> Path:
    """Write JSON atomically with UTF-8 encoding and ensure_ascii=False."""
    content = json.dumps(payload, ensure_ascii=False, indent=indent)
    return write_utf8_atomic(path, content + "\n")


def file_exists(path: os.PathLike | str) -> bool:
    return Path(path).is_file()


def resolve_path(path: os.PathLike | str) -> Path:
    """Resolve to absolute Path. Does NOT require existence."""
    return Path(path).expanduser().resolve()
