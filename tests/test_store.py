import json
import os
import stat

import pytest

from loil.record import store
from loil.errors import LoilError, SchemaError
from loil.record.project import Project

PID = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
CC = "claude-code"
SESSION_KEYS = [
    "v", "id", "agent", "project", "transcript_path", "title", "start", "end", "end_reason",
    "segments", "artifacts", "capture_state", "capture_error",
]


@pytest.fixture
def p(tmp_path):
    p = Project(tmp_path, id=PID)
    store.ensure(p)
    return p


def snapshot(root):
    out = {}
    for dp, dns, fns in os.walk(root):
        for n in dns + fns:
            path = os.path.join(dp, n)
            st = os.stat(path)
            out[os.path.relpath(path, root)] = (st.st_mtime_ns, st.st_size)
    return out


def sess(**kw):
    kw.setdefault("agent", CC)
    kw.setdefault("project", "x")
    kw.setdefault("start", "2026-08-26T00:00:00.000Z")
    return store.Session(**kw)


def sdir(p, agent, sid):
    """`<home_dir>/sessions/<agent>/<sid>/`, created (no validation: tests plant odd names)."""
    d = p.home_dir / "sessions" / agent / sid
    d.mkdir(parents=True, exist_ok=True)
    return d


def raw(p, agent, sid, text):
    (sdir(p, agent, sid) / "session.json").write_text(text)


def test_ensure_layout_and_idempotent(tmp_path, store_home):
    p = Project(tmp_path, id=PID)
    store.ensure(p)
    assert not (tmp_path / ".loil").exists()  # nothing in the repo: config/local are init's
    # central side
    assert p.home_dir == store_home / "projects" / PID
    assert sorted(x.relative_to(store_home).as_posix() for x in store_home.rglob("*")) == [
        "projects",
        f"projects/{PID}",
        f"projects/{PID}/loil.log",
        f"projects/{PID}/sessions",
    ]
    assert p.log_path.read_bytes() == b""
    before = snapshot(tmp_path), snapshot(store_home)
    store.ensure(p)
    assert (snapshot(tmp_path), snapshot(store_home)) == before


def test_ensure_needs_an_id(tmp_path, store_home):
    with pytest.raises(LoilError, match="loil init"):
        store.ensure(Project(tmp_path))
    assert not (tmp_path / ".loil").exists() and not store_home.exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores modes")
def test_ensure_repo_side_complete_when_central_fails(tmp_path, store_home):
    store_home.mkdir()
    store_home.chmod(0o500)
    try:
        with pytest.raises(LoilError, match="cannot create"):
            store.ensure(Project(tmp_path, id=PID))
    finally:
        store_home.chmod(0o700)
    p = Project(tmp_path, id=PID)
    assert not (tmp_path / ".loil").exists()
    assert not (store_home / "projects").exists()
    store.ensure(p)  # repairs
    assert p.log_path.exists()


def test_ensure_uses_pin_without_env(tmp_path, monkeypatch):
    monkeypatch.delenv("LOIL_HOME")
    monkeypatch.setenv("HOME", str(tmp_path / "h"))
    pin = tmp_path / "pin"
    p = Project(tmp_path / "r", id=PID, home=pin)
    store.ensure(p)
    assert (pin / "projects" / PID / "loil.log").exists() and not (tmp_path / "h").exists()


def test_store_dirs_are_private(p, store_home):
    for d in (store_home, store_home / "projects", p.home_dir, p.home_dir / "sessions"):
        assert stat.S_IMODE(d.stat().st_mode) == 0o700, d


def test_artifacts_dir(p):
    assert store.artifacts_dir(p, CC, "S1") == p.home_dir / "sessions" / CC / "S1"
    with pytest.raises(LoilError):
        store.artifacts_dir(p, CC, "../x")
    with pytest.raises(LoilError, match="adapter name"):
        store.artifacts_dir(p, "../x", "S1")
    assert not (p.home_dir / "sessions" / CC).exists()


def test_session_round_trip_namespaced(p):
    s = sess(id="S1", project=str(p.root))
    store.write_session(p, s)
    path = p.home_dir / "sessions" / CC / "S1" / "session.json"
    assert store.session_path(p, CC, "S1") == path == store.session_dir(p, CC, "S1") / store.SESSION_FILE
    assert store.session_dir(p, CC, "S1") == p.artifacts_dir(CC, "S1")
    d = json.loads(path.read_text())
    assert list(d) == SESSION_KEYS
    assert d["agent"] == CC
    assert d["end"] is None and d["title"] is None and d["artifacts"] == []
    back = store.read_session(p, CC, "S1")
    assert back == s and back.end is None
    assert os.listdir(path.parent) == ["session.json"]  # no temp left
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE((p.home_dir / "sessions" / CC).stat().st_mode) == 0o700
    assert not (p.home_dir / "sessions" / "S1").exists()  # never un-namespaced
    s.end = "2026-08-26T01:00:00.000Z"
    s.title = "a name"
    store.write_session(p, s)
    assert store.read_session(p, CC, "S1") == s
    with pytest.raises(LoilError, match="no session"):
        store.read_session(p, "codex", "S1")  # other namespace


def test_agent_required_and_validated(p):
    with pytest.raises(TypeError):
        store.Session(id="S1", project="x", start="t")  # type: ignore[call-arg]
    for bad in ("Claude Code", "", "../x"):
        with pytest.raises(LoilError, match="adapter name"):
            sess(id="S1", agent=bad)
    raw(p, CC, "a", json.dumps({"v": 1, "id": "a", "project": "x", "start": "t"}))
    with pytest.raises(LoilError, match="missing keys"):
        store.read_session(p, CC, "a")
    raw(p, CC, "b", json.dumps({"v": 1, "id": "b", "agent": 1, "project": "x", "start": "t"}))
    with pytest.raises(SchemaError, match="agent"):
        store.read_session(p, CC, "b")
    raw(p, CC, "c", json.dumps({"v": 1, "id": "c", "agent": "Bad Name", "project": "x", "start": "t"}))
    with pytest.raises(SchemaError, match="adapter name"):
        store.read_session(p, CC, "c")


def test_list_sessions_across_agents_sorted_and_malformed_skipped(p):
    for sid, agent, start in (
        ("b", CC, "2026-01-02T00:00:00.000Z"),
        ("a", "codex", "2026-01-03T00:00:00.000Z"),
        ("c", CC, "2026-01-01T00:00:00.000Z"),
        ("d", "codex", "2026-01-01T00:00:00.000Z"),  # same start as c: agent dir order (claude-code < codex)
    ):
        store.write_session(p, sess(id=sid, agent=agent, start=start))
    raw(p, CC, "bad", "{nope")
    raw(p, CC, "v2", '{"v": 2}')
    (sdir(p, CC, "stale") / "session.json.tmp").write_text("{}")  # no session.json: not a session
    (sdir(p, CC, "S1") / ".session.json.abc123").write_text("{}")  # an in-flight mkstemp temp, no file yet
    (sdir(p, CC, "S9") / "transcript.jsonl.gz").write_bytes(b"")  # captures whose session file is gone
    raw(p, "codex", "wrong", json.dumps(sess(id="wrong", agent=CC).to_dict()))
    raw(p, CC, "moved", json.dumps(sess(id="other").to_dict()))  # id ≠ its directory
    raw(p, "Weird Dir", "x", json.dumps(sess(id="x").to_dict()))
    (p.home_dir / "sessions" / "loose.json").write_text(json.dumps(sess(id="loose").to_dict()))  # a file, not an adapter dir
    (p.home_dir / "sessions" / CC / "flat.json").write_text(json.dumps(sess(id="flat").to_dict()))  # the pre-13f layout
    assert [(s.id, s.agent) for s in store.list_sessions(p)] == [("c", CC), ("d", "codex"), ("b", CC), ("a", "codex")]
    assert [s.id for s in store.list_sessions(p, CC)] == ["c", "b"]
    assert [s.id for s in store.list_sessions(p, "codex")] == ["d", "a"]
    assert store.list_sessions(p, "zed") == []
    with pytest.raises(LoilError, match="adapter name"):
        store.list_sessions(p, "Weird Dir")
    log = p.log_path.read_text()
    assert "claude-code/bad/session.json skipped" in log and "claude-code/v2/session.json skipped" in log
    assert "codex/wrong/session.json skipped" in log and "does not match its directory" in log
    assert "claude-code/moved/session.json skipped" in log and "id 'other' does not match" in log
    assert "Weird Dir skipped" in log
    assert "loose.json" not in log and "flat.json" not in log and "S9" not in log and "stale" not in log
    # session_ids: every session dir by name, no parsing and no stat — malformed files and
    # dirs without a file count (the evidence is here); a dir with an odd name is skipped
    assert store.session_ids(p) == {"a", "b", "c", "d", "bad", "v2", "wrong", "moved", "stale", "S1", "S9"}
    assert store.session_ids(p, CC) == {"b", "c", "bad", "v2", "moved", "stale", "S1", "S9"}
    assert store.session_ids(p, "codex") == {"a", "d", "wrong"} and store.session_ids(p, "zed") == set()
    with pytest.raises(LoilError, match="adapter name"):
        store.session_ids(p, "Weird Dir")


def test_list_sessions_without_store(tmp_path):
    assert store.list_sessions(Project(tmp_path, id=PID)) == []
    assert store.list_sessions(Project(tmp_path, id=PID), CC) == []
    assert store.session_ids(Project(tmp_path, id=PID)) == set()
    with pytest.raises(LoilError, match="loil init"):  # the store needs an id
        store.list_sessions(Project(tmp_path))


@pytest.mark.parametrize("bad", ["../x", "a/b", "a\\b", "", ".", "..", "a\x00b", "x" * 201])
def test_bad_ids(p, bad):
    with pytest.raises(LoilError):
        store.write_session(p, sess(id=bad))
    with pytest.raises(LoilError):
        store.read_session(p, CC, bad)


def test_long_id_at_the_limit_round_trips(p):
    sid = "x" * 200
    store.write_session(p, sess(id=sid))
    assert store.read_session(p, CC, sid).id == sid


def test_missing_session(p):
    with pytest.raises(LoilError):
        store.read_session(p, CC, "nope")


def test_schema_errors_on_read(p):
    raw(p, CC, "x", json.dumps({"v": 1, "id": "x", "agent": CC, "project": "p", "start": "t", "capture_state": "bogus"}))
    with pytest.raises(SchemaError):
        store.read_session(p, CC, "x")
    raw(p, CC, "y", json.dumps({"v": 2, "id": "y", "agent": CC, "project": "p", "start": "t"}))
    with pytest.raises(SchemaError):
        store.read_session(p, CC, "y")


def test_start_null_and_wrong_types_are_schema_errors(p):
    base = {"v": 1, "agent": CC, "project": "x"}
    raw(p, CC, "n", json.dumps({**base, "id": "n", "start": None}))
    raw(p, CC, "t", json.dumps({**base, "id": "t", "start": "s", "title": 1}))
    raw(p, CC, "f", json.dumps({**base, "id": "f", "start": "s", "segments": "x"}))
    assert store.list_sessions(p) == []
    for n in ("n", "t", "f"):
        with pytest.raises(SchemaError):
            store.read_session(p, CC, n)


def test_nan_never_written(p):
    with pytest.raises(LoilError):
        store.write_session(p, sess(id="a", segments=[{"at": float("nan")}]))
    assert not store.session_path(p, CC, "a").exists()


def test_failed_replace_keeps_old_file(p, monkeypatch):
    store.write_session(p, sess(id="a"))
    before = store.session_path(p, CC, "a").read_bytes()

    def boom(a, b):
        raise OSError("disk on fire")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(LoilError):
        store.write_session(p, sess(id="a", start="later"))
    assert store.session_path(p, CC, "a").read_bytes() == before
    assert os.listdir(store.session_dir(p, CC, "a")) == ["session.json"]  # temp cleaned


# --- update_session (04a) ---------------------------------------------------------------


def _s(p, sid="S1", **kw):
    kw.setdefault("start", "2026-08-26T10:00:00.000Z")
    return store.Session(id=sid, agent=CC, project=p.id, **kw)


def test_update_session_mutates_under_the_lock(p):
    store.write_session(p, _s(p))

    def fn(s):
        s.segments.append({"source": "resume", "at": "t"})
        s.end = "2026-08-26T11:00:00.000Z"

    s = store.update_session(p, CC, "S1", fn)
    assert s.end and len(s.segments) == 1
    again = store.read_session(p, CC, "S1")
    assert again.segments == [{"source": "resume", "at": "t"}] and again.end == s.end
    with pytest.raises(LoilError, match="no session"):
        store.update_session(p, CC, "S2", fn)
    assert not store.session_dir(p, CC, "S2").exists()  # nothing created for a missing session
    assert "without a lock" not in p.log_path.read_text()  # refused before the lock attempt


def test_update_session_default_creates_once(p):
    assert not (p.home_dir / "sessions" / CC).exists()  # init creates only sessions/: the first session makes the dirs
    created = store.update_session(p, CC, "S3", lambda s: None, default=_s(p, "S3"))
    assert "without a lock" not in p.log_path.read_text()
    assert store.read_session(p, CC, "S3").start == created.start
    store.update_session(p, CC, "S3", lambda s: s.segments.append({"source": "resume"}), default=_s(p, "S3"))
    assert store.read_session(p, CC, "S3").segments == [{"source": "resume"}]  # existing file wins over default
    with pytest.raises(LoilError, match="default must be"):
        store.update_session(p, CC, "S4", lambda s: None, default=_s(p, "S5"))


def test_update_session_refuses_identity_changes(p):
    store.write_session(p, _s(p))
    before = store.session_path(p, CC, "S1").read_bytes()

    def rename(s):
        s.id = "S9"

    with pytest.raises(LoilError, match="must not change"):
        store.update_session(p, CC, "S1", rename)
    assert store.session_path(p, CC, "S1").read_bytes() == before
    assert not store.session_path(p, CC, "S9").exists()


def test_update_session_without_a_lock_still_writes_and_logs(p, monkeypatch):
    import fcntl

    def no_lock(fd, op):
        raise OSError(38, "Function not implemented")

    monkeypatch.setattr(fcntl, "flock", no_lock)
    store.write_session(p, _s(p))
    store.update_session(p, CC, "S1", lambda s: setattr(s, "end", "2026-08-26T11:00:00.000Z"))
    assert store.read_session(p, CC, "S1").end == "2026-08-26T11:00:00.000Z"
    assert "proceeding without a lock" in p.log_path.read_text()


UPDATER = """
import sys
from loil.record import store
from loil.record.project import Project
p = Project(sys.argv[1], id=sys.argv[2])
mode = sys.argv[3]
def fn(s):
    if mode == "seg":
        s.segments.append({"source": "resume", "at": sys.argv[4]})
    else:
        s.end = "2026-08-26T11:00:00.000Z"
store.update_session(p, "claude-code", "S1", fn)
"""


def test_concurrent_updates_lose_nothing(p, tmp_path):
    import subprocess
    import sys

    store.write_session(p, _s(p))
    procs = []
    for i in range(20):
        mode = "seg" if i % 2 == 0 else "end"
        procs.append(
            subprocess.Popen(
                [sys.executable, "-c", UPDATER, str(p.root), p.id, mode, str(i)],
                stderr=subprocess.PIPE,
            )
        )
    for pr in procs:
        _, err = pr.communicate()
        assert pr.returncode == 0, err
    s = store.read_session(p, CC, "S1")
    assert len(s.segments) == 10 and s.end == "2026-08-26T11:00:00.000Z"
    assert [x.name for x in store.session_dir(p, CC, "S1").iterdir()] == ["session.json"]  # no temp files
    assert "without a lock" not in p.log_path.read_text()


def test_capture_fields_are_validated(p):
    d = _s(p).to_dict()
    assert d["capture_state"] is None and d["capture_error"] is None
    for bad in ({"capture_state": "bogus"}, {"capture_state": 1}):
        with pytest.raises(SchemaError, match="capture_state"):
            store.Session.from_dict({**d, **bad})
    with pytest.raises(SchemaError, match="capture_error"):
        store.Session.from_dict({**d, "capture_error": 1})
    ok = store.Session.from_dict({**d, "capture_state": "failed", "capture_error": "disk full"})
    assert ok.capture_state == "failed" and ok.capture_error == "disk full"
