"""Idempotent line management on `<root>/.gitignore`.

Never rewrites untouched lines: bytes round-trip through `surrogateescape`, other lines'
CRLF is kept, a leading BOM is ignored for matching only. Stdlib only.
"""

import os
from pathlib import Path

from loi.errors import LoiError
from loi.util.fsutil import atomic_write_bytes

_BOM = "﻿"


def _path(root) -> Path:
    return Path(root) / ".gitignore"


def is_git_checkout(root) -> bool:
    """Whether `root` is inside a git checkout: it or an ancestor holds a `.git` that is a
    file (a worktree or submodule) or a directory with a `HEAD` (an empty `.git/` is not a
    repository, and git says so). The only place loi looks at git on disk; no subprocess.
    Ancestors count: a project rooted in a subfolder of a repo is committed with it."""
    p = Path(root).absolute()
    for d in (p, *p.parents):
        g = os.path.join(d, ".git")
        # os.path.isfile, not Path.is_file: an unreadable `.git` (another user's, mode 000)
        # must read as "not a checkout", never raise into a hook
        if os.path.isfile(g) or os.path.isfile(os.path.join(g, "HEAD")):
            return True
    return False


def _read(path: Path) -> str | None:
    if not path.exists():
        return None
    try:
        return path.read_bytes().decode("utf-8", errors="surrogateescape")
    except OSError as exc:  # unreadable, or a directory named .gitignore
        raise LoiError(f"cannot read {path}: {exc.strerror or exc}") from None


def _write(path: Path, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8", errors="surrogateescape"))


def _matches(candidate: str, line: str) -> bool:
    return candidate.lstrip(_BOM).rstrip("\r").rstrip(" \t") == line  # git ignores trailing blanks


def has_line(root, line: str) -> bool:
    text = _read(_path(root))
    return text is not None and any(_matches(c, line) for c in text.split("\n"))


def ensure_line(root, line: str) -> bool:
    """Append `line` unless present. Returns True if the file changed."""
    path = _path(root)
    text = _read(path)
    if text is None:
        _write(path, line + "\n")
        return True
    if any(_matches(c, line) for c in text.split("\n")):
        return False
    if text and not text.endswith("\n"):
        text += "\n"  # otherwise the new line would glue onto the last one
    text += line + "\n"
    _write(path, text)
    return True


def remove_line(root, line: str) -> bool:
    """Drop every line equal to `line`. Returns True if the file changed."""
    path = _path(root)
    text = _read(path)
    if text is None:
        return False
    lines = text.split("\n")
    kept = [c for c in lines if not _matches(c, line)]
    if len(kept) == len(lines):
        return False
    _write(path, "\n".join(kept))
    return True
