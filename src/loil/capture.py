"""Copy a session's raw artifacts into the store (`project.artifacts_dir`).

`capture(project, session, adapter, extra=…)` — `capture_state: null` is written under the
session lock, the adapter's `sources` are copied (gzip level 1 for `.gz` destinations,
verbatim otherwise; temp sibling + rename; 0700 dirs, 0600 files), then the `extra`
sources loil generated itself (the hook payload, git state, …), then the session file gets
`artifacts` (merged by `path`), the title the scan found, and `capture_state: done`. Any
exception → `failed` + a one-line `capture_error` (logged), never raised: this runs inside
the `session-end`/`pre-compact` hooks. Idempotent: an entry with equal `bytes` + `sha256`
whose destination still exists is skipped; the source is re-hashed (a size mismatch
short-circuits straight to the copy). `bytes`/`sha256` describe the *source* stream (the
bytes themselves for a `data` source). Plans are append-only: `plans/<sha256[:12]>.md`,
one copy per distinct content. After a successful run, inventory entries whose copy is gone
from the store are dropped.

Two guards on what the agent's own directory holds: a source whose first path component is
one loil owns (`store.RESERVED`: `session.json`, `payloads/`, `git/`, `file-history/`,
`plans/`, …) is skipped and logged whatever the adapter yields, and a file over `MAX_BYTES`
is skipped and logged rather than eating the hook's timeout.

`store_sources(project, session, sources)` writes generated sources alone (session-start's
payload and git snapshot) and merges them into the inventory without touching
`capture_state`. Stdlib only (hook path).
"""

import gzip
import hashlib
import os
import stat
import tempfile

from loil import log
from loil.errors import LoilError

CHUNK = 1 << 20
GZIP_LEVEL = 1
MAX_BYTES = 200 << 20  # per file; a surprise cache under `<S>/` must not eat the hook timeout
COPIED = "copied"
UNCHANGED = "unchanged"
SKIPPED = "skipped"
#: kinds the adapter reads out of the agent's own directory; only these meet the reserved rule
AGENT_KINDS = frozenset({"transcript", "subagent", "tool-result", "other"})


def capture(project, session, adapter, *, rows=None, extra=()):
    """Returns the session as written. `rows`, if given, collects `(kind, path, status)`
    per artifact for `loil capture`'s table. `extra`: generated `Source`s stored before the
    adapter's, inside the same run."""
    from loil.record.store import update_session

    sid, agent = session.id, session.agent

    def begin(s):
        s.capture_state = None
        s.capture_error = None

    fresh = update_session(project, agent, sid, begin)
    prev = _by_path(fresh.artifacts)
    target = project.artifacts_dir(agent, sid)
    new: list[dict] = []
    error = None
    try:
        for src in _chain(extra, adapter.sources(fresh)):  # generated first: a failing adapter never loses the payload
            entry, status = _one(src, target, prev, project, agent, sid)
            if entry is None:
                if rows is not None:
                    rows.append((src.kind, src.dst_rel, SKIPPED))
                continue
            new.append(entry)
            if rows is not None:
                rows.append((entry["kind"], entry["path"], status))
    except Exception as exc:  # noqa: BLE001 — a hook never fails; the state says so
        error = _one_line(exc)
        log.get_logger(project).error("capture %s/%s failed: %s", agent, sid, error)

    def finish(s):
        merged = _by_path(s.artifacts)
        for a in new:
            merged[a["path"]] = a
        # the inventory lists what is on disk: after a *successful* run (the store was just
        # seen), an entry this run did not touch and whose copy is positively gone — a
        # wiped store — is dropped; a failed run and an unreadable store keep everything
        seen = {a["path"] for a in new}
        if error is None:
            s.artifacts = [a for a in merged.values() if a["path"] in seen or not _gone(target, a["path"])]
        else:
            s.artifacts = list(merged.values())
        if fresh.title is not None:  # the adapter set it while the transcript streamed
            s.title = fresh.title
        s.capture_state = "failed" if error else "done"
        s.capture_error = error

    try:
        return update_session(project, agent, sid, finish)
    except Exception as exc:  # noqa: BLE001 — the record side is unwritable: log, keep going
        log.get_logger(project).error("capture %s/%s: cannot write the session file: %s", agent, sid, _one_line(exc))
        return fresh


def store_sources(project, session, sources, *, rows=None):
    """Store generated `sources` for `session` and merge them into its inventory; the capture
    state is left as it is. Never raises: this runs inside session-start."""
    from loil.record.store import update_session

    sid, agent = session.id, session.agent
    target = project.artifacts_dir(agent, sid)
    prev = _by_path(session.artifacts)
    new: list[dict] = []
    try:
        for src in sources:
            entry, status = _one(src, target, prev, project, agent, sid)
            if entry is None:
                if rows is not None:
                    rows.append((src.kind, src.dst_rel, SKIPPED))
                continue
            new.append(entry)
            if rows is not None:
                rows.append((entry["kind"], entry["path"], status))
    except Exception as exc:  # noqa: BLE001
        log.get_logger(project).error("store %s/%s failed: %s", agent, sid, _one_line(exc))
    if not new:
        return session

    def merge(s):
        merged = _by_path(s.artifacts)
        for a in new:
            merged[a["path"]] = a
        s.artifacts = list(merged.values())

    try:
        return update_session(project, agent, sid, merge)
    except Exception as exc:  # noqa: BLE001
        log.get_logger(project).error("store %s/%s: cannot write the session file: %s", agent, sid, _one_line(exc))
        return session


def _chain(first, second):
    yield from first
    yield from second


def _by_path(artifacts) -> dict:
    return {a["path"]: a for a in artifacts if isinstance(a, dict) and isinstance(a.get("path"), str)}


def _one(src, target, prev, project, agent, sid):
    """One source → `(entry, status)`, or `(None, None)` when it is skipped (logged)."""
    from loil.record.store import RESERVED

    if src.kind in AGENT_KINDS and src.dst_rel is not None:
        head = src.dst_rel.split("/", 1)[0]
        if head in RESERVED:
            log.get_logger(project).warning("capture %s/%s: source %s skipped: %s is reserved", agent, sid, src.src, head)
            return None, None
    if src.src is not None:
        try:
            size = os.stat(src.src).st_size
        except OSError as exc:
            raise LoilError(f"cannot read {src.src}: {exc.strerror}") from None
        if size > MAX_BYTES:
            log.get_logger(project).warning("capture %s/%s: source %s skipped: %d bytes is over the %d MB cap", agent, sid, src.src, size, MAX_BYTES >> 20)
            return None, None
    return _capture_one(src, target, prev, project)


def _gone(target, path: str) -> bool:
    """True only when the copy at `target/path` is positively missing; unreadable is not gone."""
    try:
        return not stat.S_ISREG(os.stat(target / path).st_mode)
    except (FileNotFoundError, NotADirectoryError):
        return True
    except OSError:
        return False


def _capture_one(src, target, prev, project):
    if src.kind == "plan":
        return _capture_plan(src, target, prev, project)
    dst_rel = src.dst_rel
    dst = target / dst_rel
    gz = dst_rel.endswith(".gz")
    old = prev.get(dst_rel)
    if src.data is not None:
        digest = hashlib.sha256(src.data).hexdigest()
        if old is not None and old.get("bytes") == len(src.data) and old.get("sha256") == digest and dst.is_file():
            return dict(old), UNCHANGED
        _write_bytes(dst, src.data, gz)
        return {"kind": src.kind, "path": dst_rel, "bytes": len(src.data), "sha256": digest}, COPIED
    size = os.stat(src.src).st_size
    if old is not None and old.get("bytes") == size and dst.is_file():
        n, digest = _stream(src.src, None, False, src.feed)  # re-hash the source
        if digest == old.get("sha256"):
            return dict(old), UNCHANGED
        n, digest = _stream(src.src, dst, gz, None)  # changed at equal size: copy (already fed)
    else:
        n, digest = _stream(src.src, dst, gz, src.feed)
    return {"kind": src.kind, "path": dst_rel, "bytes": n, "sha256": digest}, COPIED


def _capture_plan(src, target, prev, project):
    """Append-only: the copy lands as `plans/<sha256[:12]>.md`; an existing file with that
    name *is* the content, so the temp copy is dropped."""
    plans = target / "plans"
    _mkdir_700(plans)
    fd, tmp = tempfile.mkstemp(dir=plans, prefix=".plan.")
    try:
        with os.fdopen(fd, "wb") as out:
            n, digest = _pump(src.src, out, None)
        dst_rel = f"plans/{digest[:12]}.md"
        dst = target / dst_rel
        entry = {"kind": "plan", "path": dst_rel, "bytes": n, "sha256": digest}
        if dst.is_file():
            os.unlink(tmp)
            return entry, UNCHANGED
        os.chmod(tmp, 0o600)
        os.replace(tmp, dst)
        return entry, COPIED
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _write_bytes(dst, data: bytes, gz: bool) -> None:
    _mkdir_700(dst.parent)
    fd, tmp = tempfile.mkstemp(dir=dst.parent, prefix=f".{dst.name}.")
    try:
        with os.fdopen(fd, "wb") as raw:
            if gz:
                with gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=GZIP_LEVEL, mtime=0) as sink:
                    sink.write(data)
            else:
                raw.write(data)
        os.chmod(tmp, 0o600)
        os.replace(tmp, dst)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _stream(src, dst, gz, feed):
    """Hash `src` while feeding `feed`; when `dst` is set also write it (gzipped if `gz`)
    to a temp sibling, then rename. Returns (bytes, sha256) of the source stream."""
    if dst is None:
        with open(src, "rb") as f:
            return _hash(f, feed)
    _mkdir_700(dst.parent)
    fd, tmp = tempfile.mkstemp(dir=dst.parent, prefix=f".{dst.name}.")
    try:
        with os.fdopen(fd, "wb") as raw:
            if gz:
                with gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=GZIP_LEVEL, mtime=0) as sink:
                    n, digest = _pump(src, sink, feed)
            else:
                n, digest = _pump(src, raw, feed)
        os.chmod(tmp, 0o600)
        os.replace(tmp, dst)
        return n, digest
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _pump(src, sink, feed):
    h = hashlib.sha256()
    n = 0
    with open(src, "rb") as f:
        while True:
            b = f.read(CHUNK)
            if not b:
                break
            h.update(b)
            n += len(b)
            if feed is not None:
                feed(b)
            sink.write(b)
    if feed is not None:
        feed(b"")  # end of stream: flush a last unterminated line
    return n, h.hexdigest()


def _hash(f, feed):
    h = hashlib.sha256()
    n = 0
    while True:
        b = f.read(CHUNK)
        if not b:
            break
        h.update(b)
        n += len(b)
        if feed is not None:
            feed(b)
    if feed is not None:
        feed(b"")
    return n, h.hexdigest()


def _mkdir_700(path):
    """`mkdir -p` with 0700 on every level created (makedirs' mode only covers the leaf)."""
    missing = []
    p = path
    while not p.is_dir():
        missing.append(p)
        if p.parent == p:
            break
        p = p.parent
    for d in reversed(missing):
        try:
            d.mkdir(mode=0o700)
        except FileExistsError:
            pass


ONE_LINE_MAX = 300


def _one_line(exc) -> str:
    """Collapse an exception to one bounded line. Over-long text is cut in the *middle*:
    error messages usually end in the path that matters, and paths are long on every OS,
    so a plain head-truncation would drop the only part a reader needs."""
    text = f"{type(exc).__name__}: {exc}" if not isinstance(exc, LoilError) else str(exc)
    text = " ".join(text.split())
    if len(text) <= ONE_LINE_MAX:
        return text
    marker = " … "
    keep = ONE_LINE_MAX - len(marker)
    head, tail = keep // 2, keep - keep // 2
    return text[:head] + marker + text[-tail:]
