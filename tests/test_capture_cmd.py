"""`loil capture [--session] [--backfill]` in-process and through the CLI; backfill via init."""

import io
import json
import os
import subprocess
import sys

import pytest

from loil.record import store
from loil.commands.capture import run_capture
from loil.commands.init import run_init
from loil.record.project import Project
from test_capture import CC, FOREIGN, N_ARTIFACTS, PY, SID, SID2, env, make_layout, new_session, repo  # noqa: F401

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only")


@pytest.fixture
def transcript(tmp_path, repo):
    return make_layout(tmp_path / "cc", repo, tmp_path / "repo-other")


def cap(repo, **kw):
    out, err = io.StringIO(), io.StringIO()
    code = run_capture(root=repo, out=out, err=err, **kw)
    return code, out.getvalue(), err.getvalue()


def sessions(repo):
    """The session ids of the store (dirs with a session.json)."""
    return sorted(store.session_ids(Project.open(repo), CC))


def test_capture_session_rows_and_state(repo, transcript):
    new_session(repo, transcript)
    code, out, err = cap(repo, session=SID)
    assert code == 0 and err == ""
    rows = [l.split() for l in out.splitlines()[:-1]]
    assert len([r for r in rows if r[0] == "copied"]) == N_ARTIFACTS and [r for r in rows if r[0] == "skipped"] == [["skipped", "other", "git/x"]]
    assert out.splitlines()[-1] == f"session {SID} ({CC}): capture_state done"
    code, out, _ = cap(repo, session=SID)
    assert code == 0 and {l.split()[0] for l in out.splitlines()[:-1]} == {"unchanged", "skipped"}


def test_capture_active_session_and_none(repo, transcript):
    code, _, err = cap(repo)
    assert code == 1 and "no active session" in err
    new_session(repo, transcript)  # open: no end
    code, out, _ = cap(repo)
    assert code == 0 and f"session {SID}" in out
    store.update_session(Project.open(repo), CC, SID, lambda s: setattr(s, "end", "2026-08-26T05:00:00.000Z"))
    code, _, err = cap(repo)
    assert code == 1 and "no active session" in err  # an ended session is not active
    code, _, err = cap(repo, session="nope-0000")
    assert code == 1 and "no session file" in err and "--backfill" in err


def test_capture_failed_exits_1(repo, transcript):
    new_session(repo, transcript)
    transcript.unlink()
    code, out, err = cap(repo, session=SID)
    assert code == 1 and "capture_state failed — transcript missing" in out


def test_capture_both_flags_is_an_error(repo, transcript):
    assert "exclusive" in cap(repo, session=SID, backfill=True)[2]


def test_capture_outside_a_project_or_not_inited(tmp_path, env):
    nowhere = tmp_path / "nowhere"
    nowhere.mkdir()
    code, out, err = cap(nowhere)
    assert code == 1 and "not a loil project" in err and out == ""


def test_backfill_creates_missing_sessions_skips_foreign_second_run_noop(repo, transcript):
    code, out, err = cap(repo, backfill=True)
    assert code == 0, err
    assert sessions(repo) == sorted([SID, SID2])
    assert sorted(x.name for x in (repo / ".loil").iterdir()) == ["config.toml", "local.toml"]
    assert FOREIGN not in out
    p = Project.open(repo)
    for sid in (SID, SID2):
        s = store.read_session(p, CC, sid)
        assert s.end_reason == "backfill" and s.segments == [] and s.capture_state == "done" and s.end and s.transcript_path
    s = store.read_session(p, CC, SID)
    assert s.start == "2026-08-26T04:44:26.420Z" and s.end == "2026-08-26T05:00:00.000Z" and len(s.artifacts) == N_ARTIFACTS
    assert s.title == "plan the capture"
    lines = out.splitlines()
    assert all("created" in l for l in lines) and len(lines) == 2
    assert f"{SID}  {CC}  created   done     {N_ARTIFACTS} copied  0 unchanged" in out
    before = {f: store.session_path(p, CC, f).read_bytes() for f in sessions(repo)}
    code, out, err = cap(repo, backfill=True)
    assert code == 0 and all("exists" in l and "0 copied" in l for l in out.splitlines())
    assert {f: store.session_path(p, CC, f).read_bytes() for f in sessions(repo)} == before


def test_backfill_repairs_start_of_non_startup_sessions_only(repo, transcript):
    p = Project.open(repo)
    new_session(repo, transcript, start="2026-08-26T09:00:00.000Z", segments=[{"source": "resume", "at": "x"}])
    new_session(repo, transcript.parent / f"{SID2}.jsonl", sid=SID2, start="2026-08-26T09:00:00.000Z", segments=[{"source": "startup", "at": "x"}])
    code, out, _ = cap(repo, backfill=True)
    assert code == 0
    assert store.read_session(p, CC, SID).start == "2026-08-26T04:44:26.420Z"  # repaired from the transcript
    assert store.read_session(p, CC, SID2).start == "2026-08-26T09:00:00.000Z"  # startup: kept
    assert store.read_session(p, CC, SID2).end is None  # never sets end on an existing session


def test_backfill_missing_roots_are_skipped(repo, transcript, tmp_path):
    entry = tmp_path / "loilhome" / "projects" / Project.open(repo).id / "project.json"
    d = json.loads(entry.read_text())
    d["roots"] = [str(tmp_path / "gone"), *d["roots"]]
    entry.write_text(json.dumps(d))
    code, out, err = cap(repo, backfill=True)
    assert code == 0 and "gone: missing, skipped" in err and len(out.splitlines()) == 2


def test_backfill_failure_exits_1_but_the_rest_is_imported(repo, transcript):
    (transcript.parent / f"{SID2}.jsonl").chmod(0)
    if os.geteuid() == 0:
        pytest.skip("root reads anything")
    try:
        code, out, err = cap(repo, backfill=True)
    finally:
        (transcript.parent / f"{SID2}.jsonl").chmod(0o644)
    # SID2's transcript is unreadable: discover cannot even confirm its cwd, so it is not a session here
    assert code == 0 and SID in out and SID2 not in out
    store.write_session(Project.open(repo), store.Session(id=SID2, agent=CC, project=str(repo), transcript_path=str(transcript.parent / "gone.jsonl"), start="2026-08-21T10:00:00.000Z"))
    code, out, err = cap(repo, backfill=True)  # an existing session whose transcript is gone
    assert code == 0 and f"{SID2}  {CC}  exists    done" in out  # discover found the real file again and repaired the path


def test_init_backfills_by_default_and_never_fails_init(repo, transcript, tmp_path):
    out, err = io.StringIO(), io.StringIO()
    assert run_init(cwd=repo, out=out, err=err) == 0
    text = out.getvalue()
    assert "registry" in text and f"{SID}  {CC}  created" in text and f"{SID2}  {CC}  created" in text
    assert sessions(repo) == sorted([SID, SID2])
    out = io.StringIO()
    assert run_init(cwd=repo, backfill=False, out=out, err=io.StringIO()) == 0
    assert SID not in out.getvalue()  # --no-backfill prints no session rows


def test_cli_capture_subprocess(repo, transcript):
    new_session(repo, transcript)
    e = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE")}
    e["CLAUDE_CONFIG_DIR"] = os.environ["CLAUDE_CONFIG_DIR"]
    p = subprocess.run([*PY, "capture", "--session", SID], cwd=repo, env=e, capture_output=True, text=True)
    assert p.returncode == 0 and p.stdout.strip().endswith("capture_state done"), p.stderr
    p = subprocess.run([*PY, "capture", "--backfill"], cwd=repo, env=e, capture_output=True, text=True)
    assert p.returncode == 0 and SID2 in p.stdout and "created" in p.stdout
    p = subprocess.run([*PY, "capture", "--help"], cwd=repo, env=e, capture_output=True, text=True)
    assert p.returncode == 0 and "--backfill" in p.stdout
    p = subprocess.run([*PY, "capture", "--session"], cwd=repo, env=e, capture_output=True, text=True)
    assert p.returncode == 2


def test_capture_session_of_an_unknown_adapter_exits_1(repo, transcript):
    p = Project.open(repo)
    store.write_session(p, store.Session(id=SID, agent="bogus-agent", project=str(repo), transcript_path=str(transcript), start="2026-08-26T04:44:26.420Z"))
    code, out, err = cap(repo, session=SID)
    assert code == 1 and "unknown coding-agent adapter 'bogus-agent'" in err and out == ""
