"""The stable `~/.local/bin/loi` shim that Claude Code hooks point at.

`loi init` writes `<shim> hook claude-code session-start` / `… session-end` / `… pre-compact` into users'
`.claude/settings.local.json` (one `hook <agent> <event>` entry per adapter hook event).
That command must keep resolving after loi is upgraded,
reinstalled or moved, so it never points at a `.venv/` or interpreter directly: it
points here, and this file is the one indirection a single `loi init` anywhere repairs.

Rules of `ensure_hook_command()`:
- the path exists and is not loi-managed (the launcher `uv tool install life-of-ideas` creates —
  a symlink — or anything else): leave it untouched and use it as-is;
- it is loi-managed (line 2 == MARKER): rewrite it to the running interpreter;
- absent: create it. Unreadable/unwritable: `LoiError`, never a silent "repair".

Any loi install that runs `init` repoints the shim to itself; a later
`uv tool install life-of-ideas` refuses while a managed shim exists (`--force` replaces it with
uv's launcher, which counts as foreign). v1 is Linux/macOS.
"""

import os
import shlex
import sys
from pathlib import Path

from loi.errors import LoiError
from loi.util.fsutil import atomic_write_bytes

MARKER = "# managed by loi init"  # forever-constant: older shims must stay recognised
SHEBANG = "#!/bin/sh"
_MARKER_LINE = MARKER.encode("ascii")
_EPHEMERAL_STORES = {"builds-v0", "archive-v0"}  # uv's ephemeral-env dirs, wherever its cache is


def shim_path() -> Path:
    return Path.home() / ".local" / "bin" / "loi"  # evaluated per call: tests set HOME


def ensure_hook_command() -> str:
    path = shim_path()
    if path.exists() or path.is_symlink():  # is_symlink catches a dangling link
        if not _is_managed(path):
            return str(path)  # foreign launcher (uv tool, …): use as-is
    _refuse_ephemeral(sys.executable)
    body = _body(sys.executable)
    if path.is_file() and path.read_bytes() == body.encode("utf-8"):
        return str(path)  # already right: no rewrite under a hook that may be exec'ing it
    _write(path, body)
    return str(path)


def _is_managed(path: Path) -> bool:
    if path.is_symlink():
        return False
    try:
        with path.open("rb") as f:  # bytes: a native launcher is foreign, not a crash
            f.readline()  # shebang
            return f.readline().rstrip(b"\n") == _MARKER_LINE
    except OSError as exc:
        raise LoiError(f"cannot read {path}: {exc}") from None


def _refuse_ephemeral(exe: str) -> None:
    exe = os.path.abspath(exe)
    caches = [
        os.environ.get("UV_CACHE_DIR"),
        os.path.join(os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")), "uv"),
        os.path.expanduser("~/Library/Caches/uv"),
    ]
    in_cache = any(
        os.path.commonpath([exe, os.path.abspath(c)]) == os.path.abspath(c)
        for c in filter(None, caches)
    )
    # `uv --no-cache` / UV_NO_CACHE=1 put the cache under $TMPDIR/.tmpXXXX instead (and a
    # `cache-dir` in uv.toml isn't in the env), so also recognise the layout:
    # <cache>/builds-v0/.tmpXXXX/bin/python and <cache>/archive-v0/<hash>/bin/python.
    if in_cache or _EPHEMERAL_STORES.intersection(exe.split(os.sep)):
        raise LoiError(
            f"{exe} lives in uv's cache and disappears when this command exits; "
            "install loi with `uv tool install life-of-ideas` or run `loi init` from its venv"
        )


def _body(exe: str) -> str:
    # exe verbatim (never realpath — that would lose the venv); shell-quoted so a path
    # with a quote, `$` or backtick can't break the exec line.
    return f'{SHEBANG}\n{MARKER}\nexec {shlex.quote(exe)} -m loi "$@"\n'


def _write(path: Path, body: str) -> None:
    # Atomic (a hook mid-exec never sees a half file) and race-safe: a fixed `loi.tmp`
    # made concurrent `loi init`s fail on each other's temp file.
    atomic_write_bytes(path, body.encode("utf-8"), mode=0o755)
