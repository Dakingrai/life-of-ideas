"""The store: `<loi_home>/projects/<id>/{loi.log, sessions/<agent>/<sid>/}` where each
session's `session.json` sits beside its captured artifacts (a session file describes
evidence only this machine has, and ships absolute paths, so it lives with that evidence).

Session files are written atomically through one chokepoint (`_write_json`). `ensure` is
idempotent and must never run from a hook. Stdlib only.
"""

import fcntl
import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from loi import log
from loi.adapters import check_agent
from loi.errors import LoiError, SchemaError
from loi.util.fsutil import atomic_write_bytes
from loi.record.project import Project, check_session_id, ensure_loi_home

V = 1
SESSION_FILE = "session.json"  # reserved: no captured artifact may land under this name
CAPTURE_STATES = (None, "done", "failed")  # null until a capture ran
#: first path components under a session dir that loi owns; a Claude-side file that
#: would land there is skipped by capture (see `capture.py`)
RESERVED = frozenset({SESSION_FILE, "payloads", "git", "file-history", "plans", "claude-project-entry.json"})


@dataclass(kw_only=True)
class Session:
    """One coding-agent session, as `<home_dir>/sessions/<agent>/<id>/session.json`.

    `project`, `transcript_path` and `capture_error` are machine-local (absolute paths).
    Writers use `update_session` (locked read-modify-write), never a bare write on a file
    another hook may be rewriting.
    """

    v: int = V
    id: str
    agent: str  # adapter name: which coding agent produced this session
    project: str
    transcript_path: str | None = None  # adapter-specific provenance; may be null
    title: str | None = None  # the name the tool gave the session (Claude Code: the last `ai-title` line)
    start: str
    end: str | None = None
    end_reason: str | None = None
    segments: list[dict] = field(default_factory=list)
    artifacts: list[dict] = field(default_factory=list)
    capture_state: str | None = None  # None | "done" | "failed"
    capture_error: str | None = None  # one line; machine-local

    def __post_init__(self) -> None:
        check_agent(self.agent)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Session":
        if not isinstance(d, dict):
            raise LoiError(f"session is not an object: {d!r}")
        v = d.get("v")
        if v != V:
            raise SchemaError(f"session declares v={v!r}; this loi understands {V}")
        if d.get("capture_state") not in CAPTURE_STATES:
            raise SchemaError(
                f"session capture_state {d.get('capture_state')!r} is not one of {CAPTURE_STATES}"
            )
        kw = {f.name: d[f.name] for f in fields(cls) if f.name in d}
        for name, types, nullable in _TYPES:
            if name in kw and not ((kw[name] is None and nullable) or type(kw[name]) in types):
                raise SchemaError(f"session {name} has the wrong type: {kw[name]!r}")
        if "agent" in kw:
            try:
                check_agent(kw["agent"])
            except LoiError as exc:
                raise SchemaError(f"session agent: {exc}") from None
        try:
            return cls(**kw)
        except TypeError as exc:
            raise LoiError(f"session missing keys: {exc}") from None


_TYPES = (  # (field, allowed types, nullable); bool is checked by exact type, not as an int
    ("id", (str,), False),
    ("agent", (str,), False),
    ("project", (str,), False),
    ("transcript_path", (str,), True),
    ("title", (str,), True),
    ("start", (str,), False),
    ("end", (str,), True),
    ("end_reason", (str,), True),
    ("segments", (list,), False),
    ("artifacts", (list,), False),
    ("capture_error", (str,), True),
)


def ensure(project: Project) -> None:
    """Create the store layout for this project: `home_dir/` 0700, `sessions/` 0700,
    `loi.log`. Idempotent: a second call changes nothing (the log is created with
    O_EXCL, never touch()'d). Nothing is created inside the repo. Needs `project.id`."""
    if project.id is None:
        raise LoiError("project id unknown; run `loi init`")
    home_dir = project.home_dir
    try:
        ensure_loi_home(project.home)
        home_dir.mkdir(mode=0o700, exist_ok=True)
        (home_dir / "sessions").mkdir(mode=0o700, exist_ok=True)
        _create_if_absent(project.log_path)
    except OSError as exc:
        raise LoiError(f"cannot create {home_dir}: {exc}") from None


def _create_if_absent(path: Path, content: bytes = b"") -> None:
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return
    try:
        if content:
            os.write(fd, content)
    finally:
        os.close(fd)


def session_path(project: Project, agent: str, session_id: str) -> Path:
    """`<home_dir>/sessions/<agent>/<sid>/session.json` — the one place; both parts are
    validated by `artifacts_dir`."""
    return project.artifacts_dir(agent, session_id) / SESSION_FILE


def session_dir(project: Project, agent: str, session_id: str) -> Path:
    """The session's directory: its `session.json` and its captured artifacts."""
    return project.artifacts_dir(agent, session_id)


def artifacts_dir(project: Project, agent: str, session_id: str) -> Path:
    """Where capture puts a session's artifacts (both parts validated)."""
    return project.artifacts_dir(agent, session_id)


def write_session(project: Project, s: Session) -> None:
    path = session_path(project, s.agent, s.id)
    mkdir_700(path.parent)
    _write_json(project, path, s.to_dict())


def mkdir_700(path: Path) -> None:
    """`mkdir -p` with 0700 on every level created (`mkdir(parents=True)` applies the mode
    to the leaf only). The store dirs hold transcripts."""
    missing = []
    d = path
    while not d.is_dir():
        missing.append(d)
        if d.parent == d:
            break
        d = d.parent
    for d in reversed(missing):
        try:
            d.mkdir(mode=0o700)
        except FileExistsError:
            pass
        except OSError as exc:
            raise LoiError(f"cannot create {d}: {exc}") from None


def read_session(project: Project, agent: str, session_id: str) -> Session:
    path = session_path(project, agent, session_id)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise LoiError(f"no session {session_id!r} for {agent}: {exc}") from None
    try:
        return Session.from_dict(json.loads(raw))
    except ValueError as exc:
        raise LoiError(f"{path}: not valid JSON: {exc}") from None


def update_session(project: Project, agent: str, session_id: str, fn, *, default: Session | None = None) -> Session:
    """Locked read-modify-write: exclusive `flock` on the session directory's fd (the
    file itself is replaced by rename, so its inode cannot carry the lock), re-read inside
    the lock, `fn(session)` mutates in place, write. A missing file starts from `default`
    when one is given (create-or-update; the directory is created 0700 first, so the
    lock has a target), else `LoiError` *before* any lock attempt. If taking the lock
    fails (`OSError`: a filesystem without locks, a vanished dir) proceed unlocked and log —
    a hook never fails on a lock."""
    path = session_path(project, agent, session_id)
    if default is not None:  # a new session: its dir must exist to be locked
        mkdir_700(path.parent)
    elif not path.is_file():
        raise LoiError(f"no session {session_id!r} for {agent}: {path} does not exist")
    fd = None
    try:
        fd = os.open(path.parent, os.O_RDONLY)
        fcntl.flock(fd, fcntl.LOCK_EX)
    except OSError as exc:
        if fd is not None:
            os.close(fd)
            fd = None
        log.get_logger(project).warning(
            "update_session %s/%s: proceeding without a lock: %s", agent, session_id, exc
        )
    try:
        if path.exists() or default is None:
            s = read_session(project, agent, session_id)
        else:
            s = default
            if s.id != session_id or s.agent != agent:
                raise LoiError("update_session: default must be the session being updated")
        fn(s)
        if s.id != session_id or s.agent != agent:
            raise LoiError("update_session: fn must not change the session's id or agent")
        write_session(project, s)
        return s
    finally:
        if fd is not None:
            os.close(fd)  # releases the lock


def _adapter_dirs(project: Project, agent: str | None) -> list[Path]:
    """`<home_dir>/sessions/<agent>/` for one adapter, or every well-formed one there
    (also adapters this loi does not know, so a newer loi's sessions still show); an
    ill-formed name is skipped and logged. Needs `project.id`."""
    root = project.home_dir / "sessions"
    if agent is not None:
        return [root / check_agent(agent)]
    try:
        names = sorted(os.listdir(root))
    except OSError:  # no store yet
        return []
    out = []
    for name in names:
        d = root / name
        if not d.is_dir():
            continue
        try:
            check_agent(name)
        except LoiError as exc:
            log.get_logger(project).warning("sessions dir %s skipped: %s", name, exc)
            continue
        out.append(d)
    return out


def adapters_present(project: Project) -> list[str]:
    """The adapter names with a well-formed `sessions/<agent>/` dir here."""
    return [d.name for d in _adapter_dirs(project, None)]


def session_ids(project: Project, agent: str | None = None) -> set[str]:
    """The ids of the sessions recorded on this machine (of `agent`, or any adapter): the
    directory names under `sessions/<agent>/`. No parsing and no per-session `stat`. A
    dir with captures but no session file counts: the evidence is here."""
    out: set[str] = set()
    for d in _adapter_dirs(project, agent):
        try:
            with os.scandir(d) as it:
                for entry in it:
                    if entry.is_dir():  # follows symlinks, as `list_sessions` does
                        out.add(entry.name)
        except OSError:
            continue
    return out


def list_sessions(project: Project, agent: str | None = None) -> list[Session]:
    """All sessions (or one adapter's), oldest `start` first, from
    `<home_dir>/sessions/<agent>/*/session.json`. A session whose `agent` or `id` field
    is not its directory's name is skipped and logged, like any malformed file; a dir
    with no `session.json` (captures whose session file is gone) is not a session."""
    out: list[Session] = []
    for d in _adapter_dirs(project, agent):
        try:
            names = sorted(os.listdir(d))
        except OSError:
            continue
        for name in names:
            path = d / name / SESSION_FILE
            if not path.is_file():
                continue
            try:
                check_session_id(name)
                s = Session.from_dict(json.loads(path.read_text(encoding="utf-8")))
                if s.agent != d.name:
                    raise LoiError(f"agent {s.agent!r} does not match its directory")
                if s.id != name:
                    raise LoiError(f"id {s.id!r} does not match its directory")
                out.append(s)
            except (OSError, ValueError, LoiError) as exc:
                log.get_logger(project).warning("session file %s/%s/%s skipped: %s", d.name, name, SESSION_FILE, exc)
    out.sort(key=lambda s: s.start)  # stable: equal starts keep (agent, dirname) order
    return out


def _write_json(project: Project, path: Path, d: dict) -> None:
    """The single JSON write path for session files. 0600: the file sits in the store
    beside the captures, which are 0600 too."""
    try:
        data = (json.dumps(d, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
    except (ValueError, TypeError) as exc:  # NaN/inf or a non-JSON value: never on disk
        raise LoiError(f"not JSON-serialisable: {exc}") from None
    atomic_write_bytes(path, data, mode=0o600)
