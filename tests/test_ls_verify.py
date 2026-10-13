"""`loil ls` and `loil verify`, in-process and through the CLI."""

import gzip
import io
import os
import subprocess
import sys

import pytest

from loil.record import store
from loil.commands.capture import run_capture
from loil.commands.ls import run_ls
from loil.commands.verify import run_verify
from loil.record.project import Project
from test_capture import CC, N_ARTIFACTS, PY, SID, SID2, env, make_layout, new_session, repo  # noqa: F401

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only")


@pytest.fixture
def transcript(tmp_path, repo):
    return make_layout(tmp_path / "cc", repo, tmp_path / "repo-other")


def run(fn, repo):
    out, err = io.StringIO(), io.StringIO()
    code = fn(root=repo, out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def test_ls_empty_then_two(repo, transcript):
    code, out, err = run(run_ls, repo)
    assert code == 0 and err == "" and out == f"loil ls: {repo} — 0 sessions in {Project.open(repo).home_dir}\n"
    assert run_capture(root=repo, backfill=True, out=io.StringIO(), err=io.StringIO()) == 0
    code, out, err = run(run_ls, repo)
    assert code == 0 and err == ""
    lines = out.splitlines()
    assert lines[0].startswith(f"loil ls: {repo} — 2 sessions in") and lines[1].startswith("session")
    assert lines[2].startswith(f"{SID2}  {CC}  2026-08-21T10:00:00.000Z  2026-08-21T10:05:00.000Z  backfill")  # oldest first
    assert lines[3].startswith(f"{SID}  {CC}  2026-08-26T04:44:26.420Z  2026-08-26T05:00:00.000Z  backfill")
    assert lines[3].endswith(f"{'done':6s}  {N_ARTIFACTS:5d}  plan the capture")
    assert "done" in lines[2] and lines[2].rstrip().endswith("1")  # SID2: the transcript alone, no title


def test_ls_outside_a_project(tmp_path, env):
    nowhere = tmp_path / "nowhere"
    nowhere.mkdir()
    code, out, err = run(run_ls, nowhere)
    assert code == 1 and "not a loil project" in err and out == ""


def test_verify_clean_then_corrupt_missing_untracked(repo, transcript):
    assert run_capture(root=repo, backfill=True, out=io.StringIO(), err=io.StringIO()) == 0
    code, out, err = run(run_verify, repo)
    assert code == 0 and err == ""
    assert out == f"loil verify: 2 sessions, {N_ARTIFACTS + 1} artifacts checked, 0 problems, 0 untracked\n"
    p = Project.open(repo)
    target = store.session_dir(p, CC, SID)
    # a corrupted gz, a plain file with other bytes, a missing copy, an untracked file
    (target / "transcript.jsonl.gz").write_bytes(gzip.compress(b"tampered\n"))
    (target / "custom-title.json").write_bytes(b'{"customTitle":"xxxxxxxx"}\n')
    (target / "subagents" / "agent-a1f227a791f782d80.meta.json").unlink()
    (target / "stray.txt").write_text("not in the inventory\n")
    code, out, err = run(run_verify, repo)
    assert code == 1
    lines = out.splitlines()
    assert any(l.startswith(f"MISMATCH   {SID}  transcript.jsonl.gz") for l in lines)
    assert any(l.startswith(f"MISMATCH   {SID}  custom-title.json") for l in lines)
    assert any(l.startswith(f"MISSING    {SID}  subagents/agent-a1f227a791f782d80.meta.json") for l in lines)
    assert any(l.startswith(f"UNTRACKED  {SID}  stray.txt") for l in lines)
    assert lines[-1] == f"loil verify: 2 sessions, {N_ARTIFACTS + 1} artifacts checked, 3 problems, 1 untracked"
    # a truncated gzip is unreadable, not a crash
    (target / "transcript.jsonl.gz").write_bytes(b"\x1f\x8b\x08\x00broken")
    code, out, _ = run(run_verify, repo)
    assert code == 1 and any(l.startswith(f"UNREADABLE {SID}  transcript.jsonl.gz") for l in out.splitlines())


def test_verify_malformed_inventory_entry(repo, transcript):
    s = new_session(repo, transcript)
    store.update_session(Project.open(repo), CC, SID, lambda x: x.artifacts.append({"kind": "other", "path": None}))
    code, out, _ = run(run_verify, repo)
    assert code == 1 and "MALFORMED" in out


def test_cli_ls_and_verify(repo, transcript):
    e = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE")}
    e["CLAUDE_CONFIG_DIR"] = os.environ["CLAUDE_CONFIG_DIR"]
    assert subprocess.run([*PY, "capture", "--backfill"], cwd=repo, env=e, capture_output=True).returncode == 0
    p = subprocess.run([*PY, "ls"], cwd=repo, env=e, capture_output=True, text=True)
    assert p.returncode == 0 and SID in p.stdout and SID2 in p.stdout, p.stderr
    p = subprocess.run([*PY, "verify"], cwd=repo, env=e, capture_output=True, text=True)
    assert p.returncode == 0 and "0 problems" in p.stdout, p.stderr
    (store.session_dir(Project.open(repo), CC, SID2) / "transcript.jsonl.gz").unlink()
    p = subprocess.run([*PY, "verify"], cwd=repo, env=e, capture_output=True, text=True)
    assert p.returncode == 1 and "MISSING" in p.stdout
    for cmd in ("ls", "verify"):
        p = subprocess.run([*PY, cmd, "--bogus"], cwd=repo, env=e, capture_output=True, text=True)
        assert p.returncode == 2
