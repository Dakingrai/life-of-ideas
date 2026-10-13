"""`loi ls` — the sessions recorded for this project, oldest first."""

import sys
from pathlib import Path
from typing import TextIO

from loi.record import store
from loi.errors import LoiError
from loi.commands.capture import open_project


def run_ls(*, root: Path | str | None = None, out: TextIO | None = None, err: TextIO | None = None) -> int:
    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr
    try:
        p = open_project(root)
    except LoiError as exc:
        print(f"loi ls: {exc}", file=err)
        return 1
    sessions = store.list_sessions(p)
    print(f"loi ls: {p.root} — {len(sessions)} session{'s' if len(sessions) != 1 else ''} in {p.home_dir}", file=out)
    if not sessions:
        return 0
    print(f"{'session':36s}  {'agent':11s}  {'start':24s}  {'end':24s}  {'reason':17s}  {'state':6s}  {'files':>5s}  title", file=out)
    for s in sessions:
        print(
            f"{s.id:36s}  {s.agent:11s}  {s.start:24s}  {s.end or '-':24s}  {s.end_reason or '-':17s}  "
            f"{s.capture_state or '-':6s}  {len(s.artifacts):5d}  {s.title or ''}".rstrip(),
            file=out,
        )
    return 0
