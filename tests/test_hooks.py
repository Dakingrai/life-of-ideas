"""`loi hook claude-code session-start|session-end|pre-compact` against an inited temp project.

Fixtures under `tests/fixtures/claude-code/` are real Claude Code payloads (2.1.246) with
`cwd`/`transcript_path` replaced by `{ROOT}`/`{TRANSCRIPT}`; the ones carrying `_note` are
hand-written from the docs (`clear|fork`, `auto`, the other end reasons). In-process tests
call the adapter's `handle`; subprocess tests go through `python -m loi hook` with `cwd` =
the repo, the payload `cwd` set, `CLAUDE*` scrubbed from the env. Nothing is ever written to
stdout: loi injects no context.
"""

import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from loi import adapters, hooks
from loi.record import store
from loi.commands.init import run_init
from loi.record.project import Project

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only")

CC = "claude-code"
SID = "eb8fb6e3-1f80-4688-a4c5-f7fc4d1e4991"  # the spike's session id, in every real fixture
FIX = Path(__file__).parent / "fixtures" / "claude-code"
PY = [sys.executable, "-m", "loi"]
TS = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")
FORBIDDEN = re.compile(r"\b(argparse|typer|click|rich|importlib\.metadata|loi\.cli)\b")
STUB_SET = ["loi", "loi.entry", "loi.errors", "loi.adapters", "loi.hooks"]
ECHO = b"loi hook: claude-code session-start\n"


# --- fixtures -------------------------------------------------------------------------


@pytest.fixture
def env(tmp_path, monkeypatch, store_home):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for var in list(os.environ):
        if var.startswith("CLAUDE") or var in ("UV_CACHE_DIR", "GIT_DIR", "GIT_WORK_TREE"):
            monkeypatch.delenv(var, raising=False)
    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir()
    (fake_bin / "claude").write_text("#!/bin/sh\nexit 0\n")
    (fake_bin / "claude").chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ['PATH']}")
    return home


def _folder(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)  # no `git init`: the root is the nearest .loi/
    return path


def _init(root: Path) -> None:
    assert run_init(cwd=root, backfill=False, out=io.StringIO(), err=io.StringIO()) == 0


@pytest.fixture
def repo(tmp_path, env):
    r = _folder(tmp_path / "repo")
    _init(r)
    return r


@pytest.fixture
def transcript(tmp_path):
    # `<config>/projects/<slug>/<S>.jsonl`, like the real thing, so `_config_dir_of` resolves
    t = tmp_path / "cc" / "projects" / "-slug" / f"{SID}.jsonl"
    t.parent.mkdir(parents=True)
    shutil.copy(FIX / "transcript-head.jsonl", t)
    return t


def payload(name: str, root: Path, transcript: Path | str | None = None, **over) -> bytes:
    d = json.loads((FIX / name).read_text())
    d["cwd"] = str(root)
    if transcript is None:
        d.pop("transcript_path")
    else:
        d["transcript_path"] = str(transcript)
    d.update(over)
    return json.dumps(d).encode()


def handle(event: str, data: bytes):
    return adapters.get(CC).handle(event, data)


def session(root: Path, sid: str = SID) -> store.Session:
    return store.read_session(Project.open(root), CC, sid)


def session_file(root: Path, sid: str = SID) -> Path:
    return store.session_path(Project.open(root), CC, sid)


def kinds(s: store.Session) -> list[str]:
    return sorted(a["kind"] for a in s.artifacts)


def non_payload(s: store.Session) -> list[dict]:
    return [a for a in s.artifacts if a["kind"] != "payload"]


def snapshot(root: Path) -> dict:
    out = {}
    for dp, _dns, fns in os.walk(root):
        for n in fns:
            p = Path(dp) / n
            out[str(p.relative_to(root))] = (p.read_bytes(), p.stat().st_mtime_ns)
    return out


def run_hook(event: str, data: bytes, cwd: Path, **extra_env) -> subprocess.CompletedProcess:
    e = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE")}
    e.update(extra_env)
    return subprocess.run([*PY, "hook", CC, event], input=data, cwd=cwd, env=e, capture_output=True)


def project_log(root: Path) -> str:
    return Project.open(root).log_path.read_text()


# --- session-start ---------------------------------------------------------------------


def test_startup_creates_session_stores_payload_prints_nothing(repo, env, transcript):
    before = snapshot(repo)
    data = payload("session-start-startup.json", repo, transcript)
    assert handle("session-start", data) is None
    s = session(repo)
    assert s.id == SID and s.agent == CC and s.project == str(repo)
    assert s.transcript_path == str(transcript) and s.title is None
    assert TS.match(s.start) and s.end is None and s.end_reason is None
    assert s.segments == [{"source": "startup", "at": s.start}]
    assert s.capture_state is None and kinds(s) == ["payload"]
    (a,) = s.artifacts
    assert re.fullmatch(r"payloads/\d{8}T\d{9}Z-session-start\.json", a["path"]) and a["bytes"] == len(data)
    assert (session_file(repo).parent / a["path"]).read_bytes() == data  # the raw payload, byte for byte
    # nothing changed in the repo: the session file is in the store, beside its captures
    assert snapshot(repo) == before
    assert session_file(repo) == Path(os.environ["LOI_HOME"]) / "projects" / Project.open(repo).id / "sessions" / CC / SID / "session.json"
    assert sorted(x.name for x in (repo / ".loi").iterdir()) == ["config.toml", "local.toml"]
    p = run_hook("session-start", payload("session-start-resume.json", repo, transcript), repo)
    assert p.returncode == 0 and p.stdout == b"" and p.stderr == ECHO


def test_resume_keeps_start_appends_segment_clears_end(repo, env, transcript):
    handle("session-start", payload("session-start-startup.json", repo, transcript))
    first = session(repo)
    handle("session-end", payload("session-end-other.json", repo, transcript))
    assert session(repo).end is not None
    time.sleep(0.002)
    handle("session-start", payload("session-start-resume.json", repo, transcript))
    s = session(repo)
    assert s.start == first.start
    assert [x["source"] for x in s.segments] == ["startup", "resume"]
    assert s.segments[1]["at"] > s.segments[0]["at"]
    assert s.end is None and s.end_reason is None
    assert kinds(s).count("payload") == 3  # start, end, start


def test_compact_is_a_segment(repo, env, transcript):
    handle("session-start", payload("session-start-startup.json", repo, transcript))
    handle("session-start", payload("session-start-compact.json", repo, transcript))
    assert [x["source"] for x in session(repo).segments] == ["startup", "compact"]


def test_resume_of_unknown_session_starts_at_the_transcripts_first_timestamp(repo, env, transcript):
    handle("session-start", payload("session-start-resume.json", repo, transcript))
    s = session(repo)
    assert s.start == "2026-08-26T18:26:48.909Z"  # line 1 of the spike transcript
    assert s.segments[0]["source"] == "resume" and s.segments[0]["at"] > s.start


@pytest.mark.parametrize("name", ["session-start-clear.json", "session-start-fork.json"])  # hand-written
def test_clear_and_fork_are_segments_only(repo, env, transcript, name):
    handle("session-start", payload("session-start-startup.json", repo, transcript))
    handle("session-start", payload(name, repo, transcript))
    s = session(repo)
    assert [x["source"] for x in s.segments] == ["startup", name.split("-")[-1][:-5]]


def test_startup_for_an_existing_id_appends_a_startup_segment(repo, env, transcript):
    # `claude -p` twice on the same id (or a startup after a crash that lost SessionEnd)
    handle("session-start", payload("session-start-startup.json", repo, transcript))
    start = session(repo).start
    handle("session-start", payload("session-start-startup.json", repo, transcript))
    s = session(repo)
    assert s.start == start and [x["source"] for x in s.segments] == ["startup", "startup"]


def test_no_transcript_path_or_missing_transcript_falls_back_to_now(repo, env, tmp_path):
    handle("session-start", payload("session-start-resume.json", repo, None))
    s = session(repo)
    assert s.transcript_path is None and TS.match(s.start) and s.start == s.segments[0]["at"]
    handle("session-start", payload("session-start-resume.json", repo, tmp_path / "gone.jsonl", session_id="S2"))
    s2 = session(repo, "S2")
    assert s2.transcript_path == str(tmp_path / "gone.jsonl") and s2.start == s2.segments[0]["at"]


def test_transcript_head_only_first_64k_and_torn_lines(repo, env, tmp_path):
    t = tmp_path / "t.jsonl"
    junk = b'{"type":"x","timestamp":' + b"1" * 70000 + b"\n"  # unparsable first line past the cut
    t.write_bytes(b'{"type":"queue-operation"}\n{"noise":1}\n' + junk + b'{"timestamp":"2026-01-01T00:00:00.000Z"}\n')
    handle("session-start", payload("session-start-resume.json", repo, t))
    assert session(repo).start == session(repo).segments[0]["at"]  # nothing parsable in the head: now
    t.write_bytes(b'{"a":1}\n{"timestamp":"2026-01-01T00:00:00+00:00","type":"user"}\n')
    handle("session-start", payload("session-start-resume.json", repo, t, session_id="S3"))
    assert session(repo, "S3").start == "2026-01-01T00:00:00.000Z"  # normalised to loi's shape


def test_transcript_path_updated_when_it_changes(repo, env, transcript, tmp_path):
    handle("session-start", payload("session-start-startup.json", repo, transcript))
    t2 = tmp_path / "t2.jsonl"
    shutil.copy(transcript, t2)
    handle("session-start", payload("session-start-resume.json", repo, t2))
    assert session(repo).transcript_path == str(t2)
    handle("session-start", payload("session-start-resume.json", repo, None))
    assert session(repo).transcript_path == str(t2)  # a payload without one keeps the last


# --- session-end / pre-compact ----------------------------------------------------------


def test_session_end_sets_end_and_reason_and_captures(repo, env, transcript):
    handle("session-start", payload("session-start-startup.json", repo, transcript))
    assert handle("session-end", payload("session-end-other.json", repo, transcript)) is None
    s = session(repo)
    assert TS.match(s.end) and s.end_reason == "other" and len(s.segments) == 1
    assert s.capture_state == "done" and s.capture_error is None
    assert [a["path"] for a in non_payload(s)] == ["transcript.jsonl.gz"]
    assert kinds(s).count("payload") == 2
    target = Project.open(repo).artifacts_dir(CC, SID)
    assert (target / "transcript.jsonl.gz").stat().st_mode & 0o777 == 0o600
    assert non_payload(s)[0]["bytes"] == transcript.stat().st_size


@pytest.mark.parametrize("reason", ["clear", "resume", "logout", "prompt_input_exit"])  # hand-written
def test_session_end_other_reasons(repo, env, transcript, reason):
    handle("session-start", payload("session-start-startup.json", repo, transcript))
    handle("session-end", payload(f"session-end-{reason}.json", repo, transcript))
    assert session(repo).end_reason == reason


def test_pre_compact_appends_segment_with_trigger_keeps_end_null_and_captures(repo, env, transcript):
    handle("session-start", payload("session-start-startup.json", repo, transcript))
    assert handle("pre-compact", payload("pre-compact-manual.json", repo, transcript)) is None
    handle("pre-compact", payload("pre-compact-auto.json", repo, transcript))  # hand-written
    s = session(repo)
    assert s.end is None and s.capture_state == "done"
    assert [(x["source"], x.get("trigger")) for x in s.segments] == [
        ("startup", None),
        ("pre-compact", "manual"),
        ("pre-compact", "auto"),
    ]
    assert all(TS.match(x["at"]) for x in s.segments)
    assert [a["path"] for a in non_payload(s)] == ["transcript.jsonl.gz"] and kinds(s).count("payload") == 3


def test_session_end_without_transcript_marks_failed_exits_0(repo, env, transcript, tmp_path):
    handle("session-start", payload("session-start-startup.json", repo, transcript))
    p = run_hook("session-end", payload("session-end-other.json", repo, tmp_path / "gone.jsonl"), repo)
    assert p.returncode == 0 and p.stdout == b""
    s = session(repo)
    assert s.end and s.capture_state == "failed" and "transcript missing" in s.capture_error
    assert kinds(s).count("payload") == 2  # the payload is still kept


@pytest.mark.parametrize("event, name", [("session-end", "session-end-other.json"), ("pre-compact", "pre-compact-manual.json")])
def test_unknown_session_is_never_created(repo, env, transcript, event, name):
    before = snapshot(repo)
    assert handle(event, payload(name, repo, transcript, session_id="never-started")) is None
    assert snapshot(repo) == before
    assert not store.session_dir(Project.open(repo), CC, "never-started").exists()
    assert f"hook claude-code {event}: LoiError(\"{event}: unknown session 'never-started'; nothing created\")" in project_log(repo)


def test_real_sequence_startup_resume_precompact_compact_end(repo, env, transcript):
    for event, name in (
        ("session-start", "session-start-startup.json"),
        ("session-end", "session-end-other.json"),
        ("session-start", "session-start-resume.json"),
        ("session-end", "session-end-other.json"),
        ("session-start", "session-start-resume.json"),
        ("pre-compact", "pre-compact-manual.json"),
        ("session-start", "session-start-compact.json"),
        ("session-end", "session-end-other.json"),
    ):
        handle(event, payload(name, repo, transcript))
    s = session(repo)
    assert [x["source"] for x in s.segments] == ["startup", "resume", "resume", "pre-compact", "compact"]
    assert s.end_reason == "other" and s.end is not None
    assert sorted(x.name for x in session_file(repo).parent.iterdir()) == ["payloads", "session.json", "transcript.jsonl.gz"]
    assert len(os.listdir(session_file(repo).parent / "payloads")) == 8 == kinds(s).count("payload")


# --- guards: nothing created, always 0 ----------------------------------------------------


def test_bad_session_id_logged_to_project_nothing_created(repo, env, transcript):
    before = snapshot(repo)
    for bad in ("../x", "a/b", "", 7, None):
        assert handle("session-start", payload("session-start-startup.json", repo, transcript, session_id=bad)) is None
    assert snapshot(repo) == before
    log = project_log(repo)
    assert log.count("hook claude-code session-start: LoiError(") == 5 and "invalid session id '../x'" in log


def test_not_inited_clone_exits_0_creates_nothing_logs_globally(tmp_path, env, store_home, transcript):
    clone = _folder(tmp_path / "clone")
    (clone / ".loi").mkdir()
    (clone / ".loi" / "config.toml").write_text('schema = 1\nid = "01ARZ3NDEKTSV4RRFFQ69G5FAV"\nname = "clone"\ncreated = "c"\nloi_version = "v"\n')  # committed, no local.toml
    store_home.mkdir()
    p = run_hook("session-start", payload("session-start-startup.json", clone, transcript), clone)
    assert p.returncode == 0 and p.stdout == b"" and p.stderr == ECHO
    assert sorted(x.name for x in (clone / ".loi").iterdir()) == ["config.toml"]
    assert "is not inited for this user (no .loi/local.toml); run `loi init`" in (store_home / "loi.log").read_text()
    assert sorted(x.name for x in store_home.iterdir()) == ["loi.log"]  # no store dir for it either


def test_store_deleted_after_init_exits_0_creates_nothing(repo, env, transcript, store_home):
    shutil.rmtree(store_home / "projects")
    p = run_hook("session-start", payload("session-start-startup.json", repo, transcript), repo)
    assert p.returncode == 0 and p.stdout == b""
    assert sorted(x.name for x in (repo / ".loi").iterdir()) == ["config.toml", "local.toml"]
    assert not (store_home / "projects").exists()  # a hook never creates the store
    assert "no store at" in (store_home / "loi.log").read_text()  # before the project is known: global log


def test_outside_a_project_and_malformed_payloads(tmp_path, env, store_home):
    store_home.mkdir()
    nowhere = tmp_path / "nowhere"
    nowhere.mkdir()
    for data in (b"not json", b"[1]", b'{"session_id": "x"}', b"\xff\xfe{}"):
        p = run_hook("session-start", data, nowhere)
        assert p.returncode == 0 and p.stdout == b"" and p.stderr == ECHO
    log = (store_home / "loi.log").read_text().splitlines()
    assert len(log) == 4 and "payload is not JSON" in log[0] and "not a JSON object" in log[1]
    assert "not a loi project here (no .loi/config.toml in this directory or above); run `loi init`" in log[2]
    assert "CLAUDE_PROJECT_DIR=None" in log[2] and "payload is not JSON" in log[3]
    assert list(nowhere.iterdir()) == []


def test_branch_without_config_exits_0_creates_nothing_logs_globally(repo, env, transcript, store_home):
    (repo / ".loi" / "config.toml").unlink()  # a branch that predates `loi init`: no longer a root
    before = snapshot(repo)
    p = run_hook("session-start", payload("session-start-startup.json", repo, transcript), repo)
    assert p.returncode == 0 and p.stdout == b"" and p.stderr == ECHO
    assert snapshot(repo) == before
    assert (repo / ".loi" / "local.toml").is_file()  # local.toml alone is not the marker
    assert "not a loi project here" in (store_home / "loi.log").read_text()


def test_unknown_event_word_logged_globally(repo, env, transcript, store_home):
    p = run_hook("bogus", payload("session-start-startup.json", repo, transcript), repo)
    assert p.returncode == 0 and p.stdout == b""
    assert "unknown hook event 'bogus'" in (store_home / "loi.log").read_text()


# --- project resolution ---------------------------------------------------------------------


def test_hook_from_a_subdirectory(repo, env, transcript):
    sub = repo / "src" / "deep"
    sub.mkdir(parents=True)
    p = run_hook("session-start", payload("session-start-startup.json", sub, transcript), sub)
    assert p.returncode == 0 and p.stdout == b""
    assert session(repo).project == str(repo)
    assert not (sub / ".loi").exists()


def test_claude_project_dir_wins_over_payload_cwd(tmp_path, env, transcript):
    a = _folder(tmp_path / "a")
    b = _folder(tmp_path / "b")
    for r in (a, b):
        _init(r)
    p = run_hook("session-start", payload("session-start-startup.json", b, transcript), b, CLAUDE_PROJECT_DIR=str(a))
    assert p.returncode == 0 and p.stdout == b""
    assert session_file(a).exists() and not session_file(b).exists()
    # CLAUDE_PROJECT_DIR outside any repo: the payload cwd is the fallback
    p = run_hook("session-end", payload("session-end-other.json", a, transcript), b, CLAUDE_PROJECT_DIR=str(tmp_path))
    assert p.returncode == 0 and session(a).end is not None


def test_no_cwd_in_payload_uses_the_process_cwd(repo, env, transcript):
    data = json.loads(payload("session-start-startup.json", repo, transcript))
    del data["cwd"]
    p = run_hook("session-start", json.dumps(data).encode(), repo)
    assert p.returncode == 0 and session_file(repo).exists()


# --- subprocess: encoding, imports, wall time -----------------------------------------------------


def test_subprocess_under_c_locale_still_records(repo, env, transcript):
    data = payload("session-start-startup.json", repo, transcript)
    for extra in ({"LANG": "C"}, {"LC_ALL": "C", "PYTHONUTF8": "0", "PYTHONCOERCECLOCALE": "0"}):
        e = {"PYTHONIOENCODING": ""}
        e.update(extra)
        p = run_hook("session-start", data, repo, **e)
        assert p.returncode == 0 and p.stderr == ECHO and p.stdout == b""
    assert len(session(repo).segments) == 2


def test_import_set_per_path(repo, env, transcript):
    def imports(data: bytes) -> list[str]:
        e = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE")}
        p = subprocess.run(
            [sys.executable, "-X", "importtime", "-m", "loi", "hook", CC, "session-start"],
            input=data,
            cwd=repo,
            env=e,
            capture_output=True,
        )
        assert p.returncode == 0
        lines = [line for line in p.stderr.decode().splitlines() if line.startswith("import time:")]
        assert [line for line in lines if FORBIDDEN.search(line)] == []
        return [line.rsplit("|", 1)[1].strip() for line in lines if "loi" in line]

    assert imports(b"") == STUB_SET
    assert imports(b"{}") == STUB_SET
    assert imports(b"  {}\n") == STUB_SET  # outer whitespace still counts as empty
    real = imports(payload("session-start-startup.json", repo, transcript))
    # `-X importtime` does not list a module loaded through `importlib.import_module`
    # (`adapters.get`), only what that module imports itself: base, store, capture, …
    assert real[:5] == STUB_SET and "loi.adapters.base" in real and "loi.capture" in real
    assert "loi.shim" not in real and not [m for m in real if m.startswith("loi.commands")]


def test_session_start_wall_time(repo, env, transcript):
    data = payload("session-start-startup.json", repo, transcript)
    times = []
    for _ in range(5):
        t0 = time.perf_counter()
        p = run_hook("session-start", data, repo)
        times.append(time.perf_counter() - t0)
        assert p.returncode == 0
    best = min(times)
    print(f"\nloi hook claude-code session-start: best {best * 1000:.0f} ms of 5 (soft budget 150 ms)")
    assert best < 0.5, f"session-start took {best:.3f}s (soft budget 0.15 s)"


@pytest.fixture
def git_repo(tmp_path, env):
    """An inited project at the top of a git repo with a few hundred committed files."""
    r = _folder(tmp_path / "gitrepo")
    subprocess.run(["git", "init", "-q", str(r)], check=True)
    for i in range(300):
        (r / f"f{i:03d}.txt").write_text(f"file {i}\n")
    subprocess.run(["git", "-C", str(r), "add", "."], check=True)
    subprocess.run(["git", "-C", str(r), "-c", "user.email=x@y", "-c", "user.name=x", "commit", "-q", "-m", "i"], check=True)
    _init(r)
    return r


def test_session_start_wall_time_with_git(git_repo, env, transcript):
    data = payload("session-start-startup.json", git_repo, transcript)
    times = []
    for _ in range(5):
        t0 = time.perf_counter()
        p = run_hook("session-start", data, git_repo)
        times.append(time.perf_counter() - t0)
        assert p.returncode == 0 and p.stdout == b""
    best = min(times)
    print(f"\nloi hook claude-code session-start (git repo, 300 files): best {best * 1000:.0f} ms of 5 (soft budget 150 ms)")
    assert best < 0.5, f"session-start with git took {best:.3f}s (soft budget 0.15 s)"


# --- concurrency -----------------------------------------------------------------------------


def test_concurrent_session_end_and_pre_compact_lose_nothing(repo, env, transcript):
    handle("session-start", payload("session-start-startup.json", repo, transcript))
    e = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE")}
    procs = []
    for i in range(20):
        event, name = ("pre-compact", "pre-compact-manual.json") if i % 2 == 0 else ("session-end", "session-end-other.json")
        pr = subprocess.Popen(
            [*PY, "hook", CC, event], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=repo, env=e
        )
        procs.append((pr, payload(name, repo, transcript)))
    for pr, data in procs:  # all 20 are alive before the first payload lands: they really race
        out, err = pr.communicate(data)
        assert pr.returncode == 0 and out == b"", err
    s = session(repo)
    assert [x["source"] for x in s.segments].count("pre-compact") == 10 and len(s.segments) == 11
    assert s.end is not None and s.end_reason == "other"
    assert s.capture_state == "done" and [a["path"] for a in non_payload(s)] == ["transcript.jsonl.gz"]
    names = sorted(x.name for x in session_file(repo).parent.iterdir())
    assert names == ["payloads", "session.json", "transcript.jsonl.gz"]  # no temp files
    assert not any(n.startswith(".") for n in os.listdir(session_file(repo).parent / "payloads"))
    assert "without a lock" not in project_log(repo)


# --- hooks.run in-process ----------------------------------------------------------------------


def test_run_in_process_prints_nothing(repo, env, transcript, monkeypatch, capfdbinary):
    class Stdin:
        def __init__(self, b):
            self.buffer = io.BytesIO(b)

        def isatty(self):
            return False

    monkeypatch.chdir(repo)
    monkeypatch.setattr(sys, "stdin", Stdin(payload("session-start-startup.json", repo, transcript)))
    assert hooks.run([CC, "session-start"]) == 0
    out, err = capfdbinary.readouterr()
    assert err == ECHO and out == b""
    monkeypatch.setattr(sys, "stdin", Stdin(payload("session-end-other.json", repo, transcript)))
    assert hooks.run([CC, "session-end"]) == 0
    assert capfdbinary.readouterr().out == b""
    assert session(repo).end is not None


def test_adapter_handle_contract():
    from loi.adapters.base import Adapter

    assert Adapter().handle("session-start", b"{}") is None
    a = adapters.get(CC)
    assert a.hook_events == (
        ("SessionStart", "session-start", None),
        ("SessionEnd", "session-end", 30),
        ("PreCompact", "pre-compact", 30),
    )


def test_closed_stdout_still_records(repo, env, transcript):
    e = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE")}
    p = subprocess.run(
        [*PY, "hook", CC, "session-start"],
        input=payload("session-start-startup.json", repo, transcript),
        cwd=repo,
        env=e,
        stderr=subprocess.PIPE,
        preexec_fn=lambda: os.close(1),  # fd 1 closed at exec: sys.stdout is None in the child
    )
    assert p.returncode == 0 and p.stderr == ECHO
    assert session_file(repo).exists() and kinds(session(repo)) == ["payload"]
    assert "ERROR" not in project_log(repo)
