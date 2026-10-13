"""The Claude Code adapter (`claude-code`). Stdlib only: `handle` runs on the hook path.

Hooks: `SessionStart` → `loil hook claude-code session-start`, `SessionEnd` → `… session-end`
(30 s), `PreCompact` → `… pre-compact` (30 s). Settings file: `<root>/.claude/settings.local.json`.

Payloads (Claude Code 2.1.x): every event carries `session_id`, `transcript_path`, `cwd`,
`hook_event_name`; SessionStart adds `source` (`startup|resume|clear|compact|fork`),
SessionEnd `reason`, PreCompact `trigger` (`manual|auto`). `session_id` is stable across
`--resume` and `/compact`, so one session file collects `segments`, one per start/compaction.

Project: `find_root($CLAUDE_PROJECT_DIR)`, else the payload's `cwd`, else the process cwd.
A checkout this user never inited (no `local.toml`) or whose store is gone is left exactly
as it is: a hook never creates `.loil/` or anything inside the repo, and never the store.
`update_session(default=)` adds `<home_dir>/sessions/claude-code/<sid>/session.json` (and
that dir) at session-start; session-end and pre-compact never create a session.

What one session's capture copies (`sources`): the transcript; every regular file under
`<transcript minus .jsonl>/` (subagents, tool results, anything else, recursively);
`<config>/file-history/<sid>/` (the versions of every file Claude edited; the mapping to
real paths is inside the transcript's `file-history-snapshot` lines); every plan file the
transcripts name. The session's title is read from the last `ai-title`/`custom-title` line
while the transcript streams by.
"""

import json
import os
from pathlib import Path

from loil.adapters.base import Adapter, Discovered, Source
from loil.errors import LoilError

NAME = "claude-code"
SETTINGS = Path(".claude") / "settings.local.json"
EVENTS = ("session-start", "session-end", "pre-compact")
HOOK_TIMEOUT = 30  # seconds for the end/compaction capture; a 10 MB transcript has been seen
TRANSCRIPT_HEAD = 64 * 1024  # the first `timestamp` line is line 1 (survey: ≤ line 5)
PLAN_KEY = b'"planFilePath"'
TITLE_KEYS = (b'"aiTitle"', b'"customTitle"')
GZIP_SUFFIXES = (".jsonl", ".txt", ".md")
KINDS = {"subagents": "subagent", "tool-results": "tool-result"}
FILE_HISTORY = "file-history"


class ClaudeCode(Adapter):
    name = NAME
    hook_events = (
        ("SessionStart", "session-start", None),
        ("SessionEnd", "session-end", HOOK_TIMEOUT),
        ("PreCompact", "pre-compact", HOOK_TIMEOUT),
    )

    def settings_path(self, root: Path) -> Path:
        return Path(root) / SETTINGS

    def sources(self, session):
        """Transcript → `transcript.jsonl.gz`; every regular file under `<S>/` (= the
        transcript path minus `.jsonl`; recursive, symlinks skipped): `subagents/**` →
        `subagent`, `tool-results/**` → `tool-result`, the rest `other`; then
        `<config>/file-history/<sid>/*` → `file-history/<name>.gz`; then every plan file the
        transcripts name — as a `planFilePath` value or as a standalone path string under
        `<config>/plans/` — found while the transcripts are streamed, `isfile`-guarded. A
        generator: the title lands on `session` once the transcript was read, the plan
        sources come last."""
        t = session.transcript_path
        if not t:
            raise LoilError("no transcript_path recorded for this session (started on another machine?)")
        if not t.endswith(".jsonl"):
            raise LoilError(f"transcript_path does not end in .jsonl: {t}")
        if not os.path.exists(t):
            raise LoilError(f"transcript missing: {t} (deleted after cleanupPeriodDays?)")
        if not os.path.isfile(t):
            raise LoilError(f"transcript_path is not a regular file: {t}")
        config = _config_dir_of(t)
        scan = TranscriptScan(os.path.join(config, "plans"))
        yield Source("transcript", t, "transcript.jsonl.gz", scan.feed)
        if scan.title is not None:  # the transcript has been pumped: the generator resumed after the yield
            session.title = scan.title
        sdir = t[: -len(".jsonl")]
        for kind, src, rel in tree_sources(sdir):
            yield Source(kind, src, rel + ".gz" if rel.endswith(GZIP_SUFFIXES) else rel, scan.feed if src.endswith(".jsonl") else None)
        for _kind, src, rel in tree_sources(os.path.join(config, FILE_HISTORY, session.id)):
            yield Source(FILE_HISTORY, src, f"{FILE_HISTORY}/{rel}.gz")
        for path in scan.paths() + scan.string_paths():  # key values, then the tool-call path strings
            if os.path.isfile(path):
                yield Source("plan", path, None)

    def discover(self, root):
        """Sessions under `$CLAUDE_CONFIG_DIR or ~/.claude` `/projects/<slug>*/`: every
        `<S>.jsonl` (not `agent-*.jsonl`) whose own `cwd` field resolves to `root`."""
        root = Path(root).absolute()
        projects = os.path.join(_config_dir(), "projects")
        slug = _slug(str(root))
        from loil.record.project import check_session_id, find_root

        out = []
        try:
            dirs = sorted(d for d in os.listdir(projects) if d.startswith(slug))
        except OSError:
            return out
        for d in dirs:
            full = os.path.join(projects, d)
            try:
                names = sorted(os.listdir(full))
            except OSError:
                continue
            for name in names:
                if not name.endswith(".jsonl") or name.startswith("agent-"):
                    continue
                path = os.path.join(full, name)
                if not os.path.isfile(path) or os.path.islink(path):
                    continue
                sid = name[: -len(".jsonl")]
                try:
                    check_session_id(sid)
                except LoilError:
                    continue
                cwd, first, last = _scan_transcript(path)
                if cwd is None or find_root(cwd) != root:
                    continue
                out.append(Discovered(sid, path, first, last))
        out.sort(key=lambda d: (d.start or "", d.id))
        return out

    def handle(self, event: str, payload: bytes) -> None:
        if event not in EVENTS:
            raise LoilError(f"unknown hook event {event!r} for {NAME}; expected one of {', '.join(EVENTS)}")
        d = _parse(payload)
        project, local = _resolve(d)  # raises before the project is known → global log
        from loil import log

        try:
            from loil.record.project import check_session_id

            sid = check_session_id(d.get("session_id"))
            _HANDLERS[event](project, local, d, sid, payload)
        except Exception as exc:  # noqa: BLE001 — logged to the project, never out of a hook
            log.get_logger(project).error("hook %s %s: %r", NAME, event, exc)
        return None


ADAPTER = ClaudeCode()


def _parse(payload: bytes) -> dict:
    try:
        d = json.loads(payload)
    except ValueError as exc:  # also UnicodeDecodeError
        raise LoilError(f"payload is not JSON: {exc}") from None
    if not isinstance(d, dict):
        raise LoilError(f"payload is not a JSON object: {type(d).__name__}")
    return d


def _str(d: dict, key: str) -> str | None:
    v = d.get(key)
    return v if isinstance(v, str) and v else None


def _resolve(d: dict):
    from loil.record.config import load_config, load_local
    from loil.record.project import NOT_A_PROJECT, Project, find_root

    env = os.environ.get("CLAUDE_PROJECT_DIR")
    root = find_root(env) if env else None
    if root is None:
        root = find_root(_str(d, "cwd"))  # None → the hook's own cwd
    if root is None:
        raise LoilError(f"{NOT_A_PROJECT} (CLAUDE_PROJECT_DIR={env!r}, cwd={d.get('cwd')!r})")
    p = Project(root)
    if not p.local_path.is_file():
        raise LoilError(f"{root} is not inited for this user (no .loil/local.toml); run `loil init`")
    local = load_local(p)
    cfg = load_config(p)
    project = Project(root, id=cfg.id, home=local.home)
    if not project.home_dir.is_dir():  # a hook never creates the store
        raise LoilError(f"no store at {project.home_dir}; run `loil init`")
    return project, local


def _payload_source(event: str, payload: bytes) -> Source:
    from loil.util import clock

    stamp = clock.now_iso().replace("-", "").replace(":", "").replace(".", "")  # 20261012T204530123Z
    return Source("payload", None, f"payloads/{stamp}-{event}.json", data=payload)


def _session_start(project, local, d: dict, sid: str, payload: bytes) -> None:
    from loil.util import clock
    from loil.record.store import Session, update_session
    from loil.capture import store_sources

    source = _str(d, "source") or "unknown"
    transcript = _str(d, "transcript_path")
    now = clock.now_iso()
    start = now if source == "startup" else (_first_timestamp(transcript) or now)
    default = Session(id=sid, agent=NAME, project=str(project.root), transcript_path=transcript, start=start)

    def fn(s: Session) -> None:
        s.segments.append({"source": source, "at": now})
        s.end = None
        s.end_reason = None
        if transcript:
            s.transcript_path = transcript

    s = update_session(project, NAME, sid, fn, default=default)
    store_sources(project, s, [_payload_source("session-start", payload), *_git_start(project)])
    _snapshots(project, s, "session-start", d, full=False)


def _session_end(project, local, d: dict, sid: str, payload: bytes) -> None:
    from loil.util import clock
    from loil.record.store import update_session

    now = clock.now_iso()
    reason = _str(d, "reason")
    transcript = _str(d, "transcript_path")

    def fn(s) -> None:
        s.end = now
        s.end_reason = reason
        if transcript:
            s.transcript_path = transcript

    s = _update_known(project, sid, "session-end", fn, update_session)
    _capture(project, local, s, "session-end", payload)


def _pre_compact(project, local, d: dict, sid: str, payload: bytes) -> None:
    from loil.util import clock
    from loil.record.store import update_session

    seg = {"source": "pre-compact", "at": clock.now_iso(), "trigger": _str(d, "trigger")}
    transcript = _str(d, "transcript_path")

    def fn(s) -> None:
        s.segments.append(seg)
        if transcript:
            s.transcript_path = transcript

    s = _update_known(project, sid, "pre-compact", fn, update_session)
    _capture(project, local, s, "pre-compact", payload)


def _capture(project, local, s, event: str, payload: bytes) -> None:
    from loil.capture import capture  # gzip/hashlib/tempfile: only here, never at session-start

    capture(project, s, ADAPTER, extra=[_payload_source(event, payload), *_git_end(project), *_claude_entry(project, s, d=None)])
    _snapshots(project, s, event, None, full=True)


def _git_start(project) -> list:
    from loil import gitstate

    data = gitstate.start_json(project.root)
    return [Source("git", None, "git/start.json", data=data)] if data else []


def _git_end(project) -> list:
    from loil import gitstate

    end, diff = gitstate.end_json_and_diff(project.root)
    out = []
    if end:
        out.append(Source("git", None, "git/end.json", data=end))
    if diff:
        out.append(Source("git", None, "git/diff-end.patch.gz", data=diff))
    return out


def _claude_entry(project, s, d) -> list:
    from loil import snapshots

    config = _config_dir_of(s.transcript_path) if s.transcript_path else _config_dir()
    data = snapshots.claude_project_entry(config, [str(project.root), s.project])
    return [Source("claude-project-entry", None, "claude-project-entry.json", data=data)] if data else []


def _snapshots(project, s, event: str, d, *, full: bool) -> None:
    from loil import snapshots

    config = _config_dir_of(s.transcript_path) if s.transcript_path else None
    snapshots.run(project, s, event, config_dir=config, full=full)


def _update_known(project, sid: str, event: str, fn, update_session):
    # No `default`: an unknown session is never created here (backfill owns that). The
    # pre-check only buys a clean one-line log instead of update_session's two.
    from loil.record.store import session_path

    if not session_path(project, NAME, sid).is_file():
        raise LoilError(f"{event}: unknown session {sid!r}; nothing created")
    return update_session(project, NAME, sid, fn)


_HANDLERS = {"session-start": _session_start, "session-end": _session_end, "pre-compact": _pre_compact}


def _first_timestamp(transcript: str | None) -> str | None:
    """The first `timestamp` in the transcript's head, in loil's shape; None when there is
    no transcript yet (a fresh `startup`), it is unreadable, or no line parses."""
    if not transcript:
        return None
    try:
        with open(transcript, "rb") as f:
            head = f.read(TRANSCRIPT_HEAD)
    except OSError:
        return None
    from loil.util import clock

    for raw in head.split(b"\n"):
        if b'"timestamp"' not in raw:
            continue
        try:
            line = json.loads(raw)
        except ValueError:  # a torn tail past the 64 KB cut, or not JSON
            continue
        ts = line.get("timestamp") if isinstance(line, dict) else None
        if isinstance(ts, str):
            try:
                return clock.now_iso(int(clock.parse_iso(ts).timestamp() * 1000))
            except (ValueError, OverflowError, OSError):
                continue
    return None


# --- sources / discover helpers ----------------------------------------------------------------


def _config_dir() -> str:
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")


def _config_dir_of(transcript: str) -> str:
    """The config dir a transcript at `<config>/projects/<slug>/<S>.jsonl` lives in — not the
    env (a backfilled session may predate a `CLAUDE_CONFIG_DIR` change)."""
    return os.path.dirname(os.path.dirname(os.path.dirname(transcript)))


def _plans_dir(transcript: str) -> str:
    return os.path.join(_config_dir_of(transcript), "plans")


def _slug(path: str) -> str:
    """Claude Code's project-dir name: every non-alphanumeric byte of the cwd → `-`."""
    return "".join(c if c.isascii() and c.isalnum() else "-" for c in path)


def tree_sources(top: str):
    """Every regular file under `top`, recursively, as `(kind, abs_path, rel_posix)`, sorted
    for a deterministic inventory. Symlinks (to files or dirs) are never followed; `kind` is
    `KINDS[<first path component>]` or `other`. A missing or unreadable `top` yields nothing."""
    if not os.path.isdir(top) or os.path.islink(top):
        return
    for dp, dns, fns in os.walk(top, followlinks=False):
        dns[:] = sorted(d for d in dns if not os.path.islink(os.path.join(dp, d)))
        for name in sorted(fns):
            full = os.path.join(dp, name)
            if os.path.islink(full) or not os.path.isfile(full):
                continue
            rel = os.path.relpath(full, top).replace(os.sep, "/")
            yield KINDS.get(rel.split("/", 1)[0] if "/" in rel else "", "other"), full, rel


class TranscriptScan:
    """Fed every chunk of the transcripts as they stream. Collects the distinct `planFilePath`
    values (bytes pre-filter, JSON parse of the hits only; the key may sit under
    `attachment`), the standalone path strings under `<config>/plans/` that tool calls on
    subagent-written plans leave (`input.file_path`, `toolUseResult.file.filePath`), and the
    session's title: the last `customTitle` seen, else the last `aiTitle`."""

    def __init__(self, plans_dir: str):
        self.plans_dir = plans_dir
        self._tail = b""
        self._key: dict[str, None] = {}
        self._strings: dict[str, None] = {}
        self._needle = ('"' + plans_dir + "/").encode("utf-8", "surrogateescape")
        self._ai_title: str | None = None
        self._custom_title: str | None = None

    @property
    def title(self) -> str | None:
        return self._custom_title or self._ai_title

    def feed(self, chunk: bytes) -> None:
        if not chunk:  # end of one file: a last unterminated line
            if self._tail:
                self._line(self._tail)
                self._tail = b""
            return
        data = self._tail + chunk if self._tail else chunk
        cut = data.rfind(b"\n")
        if cut < 0:
            self._tail = data
            return
        body, self._tail = data[:cut], data[cut + 1:]
        if PLAN_KEY in body or self._needle in body or any(k in body for k in TITLE_KEYS):
            for line in body.split(b"\n"):
                if PLAN_KEY in line or self._needle in line or any(k in line for k in TITLE_KEYS):
                    self._line(line)

    def _line(self, line: bytes) -> None:
        if PLAN_KEY in line or any(k in line for k in TITLE_KEYS):
            try:
                d = json.loads(line)
            except ValueError:
                d = None
            if isinstance(d, dict):
                if PLAN_KEY in line:
                    for v in _dig(d, "planFilePath"):
                        if isinstance(v, str):
                            self._key[v] = None
                t = d.get("customTitle")
                if isinstance(t, str) and t.strip():
                    self._custom_title = t.strip()
                t = d.get("aiTitle")
                if isinstance(t, str) and t.strip():
                    self._ai_title = t.strip()
        if self._needle in line:
            start = 0
            while True:
                i = line.find(self._needle, start)
                if i < 0:
                    break
                j = line.find(b'"', i + 1)
                if j < 0:
                    break
                v = line[i + 1:j]
                if v.endswith(b".md") and b"\\" not in v and b"/" not in v[len(self._needle) - 1:]:
                    self._strings[v.decode("utf-8", "surrogateescape")] = None
                start = j + 1

    def paths(self) -> list[str]:
        return list(self._key)

    def string_paths(self) -> list[str]:
        return [p for p in self._strings if p not in self._key]


PlanScan = TranscriptScan  # the name loi's tests use


def _dig(d, key):
    if isinstance(d, dict):
        if key in d:
            yield d[key]
        for v in d.values():
            if isinstance(v, (dict, list)):
                yield from _dig(v, key)
    elif isinstance(d, list):
        for v in d:
            if isinstance(v, (dict, list)):
                yield from _dig(v, key)


def _scan_transcript(path: str):
    """One pass: the top-level `cwd` of the first line carrying one, and the `timestamp`
    of the first and last lines carrying one, in file order (loil's shape)."""
    cwd = first = None
    last_line = None
    try:
        with open(path, "rb") as f:
            for raw in f:
                if cwd is None and b'"cwd"' in raw:
                    d = _loads(raw)
                    if isinstance(d, dict) and isinstance(d.get("cwd"), str):
                        cwd = d["cwd"]
                if b'"timestamp"' in raw:
                    if first is None:
                        first = _ts(raw)
                    last_line = raw
    except OSError:
        return None, None, None
    return cwd, first, _ts(last_line) if last_line is not None else None


def _loads(raw: bytes):
    try:
        return json.loads(raw)
    except ValueError:
        return None


def _ts(raw: bytes) -> str | None:
    d = _loads(raw)
    ts = d.get("timestamp") if isinstance(d, dict) else None
    if not isinstance(ts, str):
        return None
    from loil.util import clock

    try:
        return clock.now_iso(int(clock.parse_iso(ts).timestamp() * 1000))
    except (ValueError, OverflowError, OSError):
        return None
