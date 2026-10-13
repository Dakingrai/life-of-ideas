"""`loil verify` — re-hash every stored artifact of every session against its session file.

For each inventory entry: the copy must exist and, read back (gunzipped when its name ends
in `.gz`), hash to the recorded `sha256` with the recorded `bytes`. Files in a session dir
that no inventory names (other than `session.json`) are listed as `untracked`: not a
failure, but not verified either. Exit 1 when anything is missing or mismatched."""

import gzip
import hashlib
import os
import sys
from pathlib import Path
from typing import TextIO

from loil.record import store
from loil.errors import LoilError
from loil.commands.capture import open_project

CHUNK = 1 << 20


def run_verify(*, root: Path | str | None = None, out: TextIO | None = None, err: TextIO | None = None) -> int:
    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr
    try:
        p = open_project(root)
    except LoilError as exc:
        print(f"loil verify: {exc}", file=err)
        return 1
    sessions = store.list_sessions(p)
    problems = 0
    artifacts = 0
    untracked = 0
    for s in sessions:
        target = store.session_dir(p, s.agent, s.id)
        listed = {store.SESSION_FILE}
        for a in s.artifacts:
            if not isinstance(a, dict) or not isinstance(a.get("path"), str):
                problems += 1
                print(f"MALFORMED  {s.id}  {a!r}", file=out)
                continue
            artifacts += 1
            listed.add(a["path"])
            path = target / a["path"]
            if not path.is_file():
                problems += 1
                print(f"MISSING    {s.id}  {a['path']}", file=out)
                continue
            try:
                n, digest = _hash(path, gz=a["path"].endswith(".gz"))
            except (OSError, EOFError, gzip.BadGzipFile) as exc:
                problems += 1
                print(f"UNREADABLE {s.id}  {a['path']}  {exc}", file=out)
                continue
            if n != a.get("bytes") or digest != a.get("sha256"):
                problems += 1
                print(f"MISMATCH   {s.id}  {a['path']}  {n} bytes {digest[:12]} (recorded {a.get('bytes')} {str(a.get('sha256'))[:12]})", file=out)
        for dp, _dns, fns in os.walk(target):
            for name in fns:
                rel = os.path.relpath(os.path.join(dp, name), target).replace(os.sep, "/")
                if rel not in listed and not name.startswith("."):
                    untracked += 1
                    print(f"UNTRACKED  {s.id}  {rel}", file=out)
    print(
        f"loil verify: {len(sessions)} session{'s' if len(sessions) != 1 else ''}, {artifacts} artifact{'s' if artifacts != 1 else ''} checked, "
        f"{problems} problem{'s' if problems != 1 else ''}, {untracked} untracked",
        file=out,
    )
    return 1 if problems else 0


def _hash(path: Path, *, gz: bool) -> tuple[int, str]:
    h = hashlib.sha256()
    n = 0
    opener = gzip.open if gz else open
    with opener(path, "rb") as f:
        while True:
            b = f.read(CHUNK)
            if not b:
                break
            h.update(b)
            n += len(b)
    return n, h.hexdigest()
