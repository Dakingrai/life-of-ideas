"""`loi capture [--session ID | --backfill]` — the explicit checkpoint (what the hooks do at
SessionEnd/PreCompact, by hand) and the import of the sessions a tool still holds on disk
from before loi existed. `backfill_rows` is also what `loi init` runs by default."""

import os
import sys
from pathlib import Path
from typing import TextIO

from loi import adapters
from loi.record import registry, store
from loi.util import clock
from loi.capture import capture
from loi.errors import LoiError
from loi.record.project import NOT_A_PROJECT, Project, check_session_id, find_root

BACKFILL = "backfill"


def run_capture(
    *,
    session: str | None = None,
    backfill: bool = False,
    root: Path | str | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr
    try:
        p = open_project(root)
        if backfill:
            if session is not None:
                raise LoiError("--session and --backfill are exclusive")
            failed = backfill_rows(p, list(adapters.KNOWN), out, err)
            return 1 if failed else 0
        if session is not None:
            check_session_id(session)
            s = _find_session(p, session)
        else:
            s = _active_session(p)
        ad = adapters.get(s.agent)  # a session dir of an adapter this loi lacks: say so, exit 1
    except LoiError as exc:
        print(f"loi capture: {exc}", file=err)
        return 1
    rows: list[tuple[str, str, str]] = []
    s = capture(p, s, ad, rows=rows)
    for kind, path, status in rows:
        print(f"{status:9s} {kind:12s} {path}", file=out)
    print(f"session {s.id} ({s.agent}): capture_state {s.capture_state}" + (f" — {s.capture_error}" if s.capture_error else ""), file=out)
    return 1 if s.capture_state == "failed" else 0


def open_project(root) -> Project:
    top = find_root(root)
    if top is None:
        raise LoiError(f"{root}: {NOT_A_PROJECT}" if root else NOT_A_PROJECT)
    p = Project.open(top)
    if not p.local_path.is_file():
        raise LoiError(f"{top} is not inited for this user (no .loi/local.toml); run `loi init`")
    if not p.home_dir.is_dir():
        raise LoiError(f"no store at {p.home_dir}; run `loi init`")
    return p


def _find_session(p: Project, sid: str) -> store.Session:
    for name in [d.name for d in store._adapter_dirs(p, None)]:
        if store.session_path(p, name, sid).is_file():
            return store.read_session(p, name, sid)
    raise LoiError(f"no session file for {sid!r}; `loi capture --backfill` creates one from the tool's transcript")


def _active_session(p: Project) -> store.Session:
    """The newest session with no `end` (a live one, or one whose SessionEnd never fired)."""
    open_ = [s for s in store.list_sessions(p) if s.end is None]
    if not open_:
        raise LoiError("no active session; give --session <id>")
    return open_[-1]


def backfill_rows(p: Project, agent_names: list[str], out: TextIO, err: TextIO) -> int:
    """Import every session each named adapter still holds for every root this project was
    inited from. One row per session on `out`; a missing root on `err`. Returns the number of
    sessions whose capture failed."""
    entry = registry.entry(p)
    roots = list((entry or {}).get("roots") or [str(p.root)])
    failed = 0
    for root in roots:
        if not os.path.isdir(root):
            print(f"{root}: missing, skipped", file=err)
            continue
        rp = Project(root, id=p.id, home=p.home)
        for name in agent_names:
            ad = adapters.get(name)
            for d in ad.discover(root):
                path = store.session_path(rp, name, d.id)
                if path.is_file():
                    action = "exists"

                    def fn(s, d=d):
                        if not s.segments or s.segments[0].get("source") != "startup":
                            if d.start:
                                s.start = d.start
                        if s.transcript_path is None or not os.path.isfile(s.transcript_path):
                            s.transcript_path = d.transcript_path  # the file discover found is the live one

                    s = store.update_session(rp, name, d.id, fn)
                else:
                    action = "created"
                    now = clock.now_iso()
                    s = store.Session(
                        id=d.id, agent=name, project=str(rp.root), transcript_path=d.transcript_path,
                        start=d.start or now, end=d.end or now, end_reason=BACKFILL, segments=[],
                    )
                    s = store.update_session(rp, name, d.id, lambda s: None, default=s)
                rows: list = []
                s = capture(rp, s, ad, rows=rows)
                copied = sum(1 for r in rows if r[2] == "copied")
                unchanged = sum(1 for r in rows if r[2] == "unchanged")
                if s.capture_state == "failed":
                    failed += 1
                print(
                    f"{d.id}  {name}  {action:8s}  {s.capture_state or '-':7s}  {copied} copied  {unchanged} unchanged"
                    + (f"  {s.capture_error}" if s.capture_error else ""),
                    file=out,
                )
    return failed
