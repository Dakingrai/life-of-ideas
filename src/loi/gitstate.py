"""The repo's git state at a hook, as bytes for `git/start.json`, `git/end.json` and
`git/diff-end.patch.gz`. Stdlib only; runs inside the hooks.

Read-only by construction: every call sets `GIT_OPTIONAL_LOCKS=0` (so `git status` never
refreshes `.git/index`, which would be a write into the repo), runs with a short timeout,
and any failure — no git on PATH, not a repository, a timeout — yields `None` fields rather
than an error. `is_git_checkout` (a pure file check) gates everything, so a plain folder
costs no subprocess at all.

Session-start records HEAD and the branch only (two fast `rev-parse`s). Session-end and
pre-compact add `git status --porcelain` and the diff against HEAD, capped at `DIFF_CAP`
bytes with `diff_truncated` saying so: the only record of uncommitted work a session left.
"""

import json
import os
import subprocess

from loi.util import clock
from loi.util.gitignore import is_git_checkout

TIMEOUT = 2  # seconds per git call
DIFF_CAP = 5 << 20


def start_json(root) -> bytes | None:
    """`{"at", "head", "branch"}`, or None when `root` is not inside a git checkout."""
    if not is_git_checkout(root):
        return None
    return _dumps({"at": clock.now_iso(), **_head_branch(root)})


def end_json_and_diff(root, cap: int = DIFF_CAP) -> tuple[bytes | None, bytes | None]:
    """`({"at", "head", "branch", "status_porcelain", "diff_bytes", "diff_truncated"},
    diff)`; `(None, None)` outside a git checkout. `diff` is None when git gave none (an
    error, or nothing changed)."""
    if not is_git_checkout(root):
        return None, None
    hb = _head_branch(root)
    status = _run(root, ["status", "--porcelain", "--untracked-files=all"])
    diff = _run(root, ["diff", "HEAD", "--no-color", "--no-ext-diff"], max_bytes=cap + 1)
    if diff is None and hb.get("head") is None:
        diff = _run(root, ["diff", "--no-color", "--no-ext-diff"], max_bytes=cap + 1)  # an unborn branch: no HEAD to diff against
    truncated = diff is not None and len(diff) > cap
    if truncated:
        diff = diff[:cap]
    end = {
        "at": clock.now_iso(),
        **hb,
        "status_porcelain": status.decode("utf-8", "replace").splitlines() if status is not None else None,
        "diff_bytes": len(diff) if diff is not None else None,
        "diff_truncated": truncated,
    }
    return _dumps(end), (diff if diff else None)


def _head_branch(root) -> dict:
    head = _run(root, ["rev-parse", "--verify", "-q", "HEAD"])
    branch = _run(root, ["rev-parse", "--abbrev-ref", "HEAD"])
    return {
        "head": head.decode("ascii", "replace").strip() if head else None,  # None on an unborn branch
        "branch": branch.decode("utf-8", "replace").strip() if branch else None,  # "HEAD" when detached
    }


def _run(root, args: list[str], *, max_bytes: int | None = None) -> bytes | None:
    env = {k: v for k, v in os.environ.items() if k not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")}
    env["GIT_OPTIONAL_LOCKS"] = "0"
    try:
        p = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            timeout=TIMEOUT,
            env=env,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):  # no git, a timeout, a vanished root
        return None
    if p.returncode != 0:
        return None
    out = p.stdout
    return out if max_bytes is None else out[:max_bytes]


def _dumps(d: dict) -> bytes:
    return (json.dumps(d, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
