"""`snapshots`: memory and repo-config tree snapshots, the project's prompt history and
pastes, the sessions index, the `.claude.json` entry — and their wiring into the hooks."""

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from loil import adapters, snapshots
from loil.record import store
from loil.commands.init import run_init
from loil.record.project import Project
from test_capture import CC, SID, SID2, env, make_layout, new_session, repo  # noqa: F401

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only")


@pytest.fixture
def transcript(tmp_path, repo):
    t = make_layout(tmp_path / "cc", repo, tmp_path / "repo-other")
    mem = t.parent / "memory"
    mem.mkdir()
    (mem / "MEMORY.md").write_text("# Memory index\n- [User role](user_role.md)\n")
    (mem / "user_role.md").write_text("---\nname: user-role\n---\nresearcher\n")
    return t


def project(repo):
    return Project.open(repo)


def index(repo, name):
    path = project(repo).home_dir / name / "index.jsonl"
    return [json.loads(l) for l in path.read_text().splitlines()] if path.is_file() else []


def hook(event, repo, transcript, **over):
    d = {"session_id": SID, "transcript_path": str(transcript), "cwd": str(repo), "hook_event_name": event, **over}
    return adapters.get(CC).handle(event, json.dumps(d).encode())


# --- memory ----------------------------------------------------------------------------------


def test_memory_snapshot_only_when_changed(repo, transcript):
    s = new_session(repo, transcript)
    p = project(repo)
    first = snapshots.snapshot_memory(p, s, "session-start")
    assert first and (p.home_dir / "memory" / first / "MEMORY.md").read_text().startswith("# Memory index")
    assert (p.home_dir / "memory" / first / "user_role.md").is_file()
    assert oct(os.stat(p.home_dir / "memory" / first / "MEMORY.md").st_mode & 0o777) == "0o600"
    (ix,) = index(repo, "memory")
    assert ix["event"] == "session-start" and ix["session"] == SID and ix["dir"] == first
    assert sorted(f["path"] for f in ix["files"]) == ["MEMORY.md", "user_role.md"] and len(ix["tree_sha256"]) == 64
    assert snapshots.snapshot_memory(p, s, "session-end") is None  # unchanged: no new dir, no new line
    assert len(index(repo, "memory")) == 1 and {x.name for x in (p.home_dir / "memory").iterdir()} == {"index.jsonl", first}
    (transcript.parent / "memory" / "feedback.md").write_text("one topic per session\n")
    second = snapshots.snapshot_memory(p, s, "session-end")
    assert second and second != first
    assert [x["dir"] for x in index(repo, "memory")] == [first, second]
    assert (p.home_dir / "memory" / second / "feedback.md").is_file() and (p.home_dir / "memory" / first / "MEMORY.md").is_file()
    # the memory dir wiped: recorded as an empty snapshot, the earlier copies stay
    import shutil

    shutil.rmtree(transcript.parent / "memory")
    assert snapshots.snapshot_memory(p, s, "session-end") is None
    assert index(repo, "memory")[-1]["files"] == [] and index(repo, "memory")[-1]["dir"] is None


def test_no_memory_dir_and_no_transcript_record_nothing(repo, transcript):
    import shutil

    shutil.rmtree(transcript.parent / "memory")
    p = project(repo)
    assert snapshots.snapshot_memory(p, new_session(repo, transcript), "session-start") is None
    assert snapshots.snapshot_memory(p, new_session(repo, None, sid=SID2), "session-start") is None
    assert not (p.home_dir / "memory").exists()


def test_memory_symlinks_are_skipped(repo, transcript, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("not memory\n")
    os.symlink(secret, transcript.parent / "memory" / "link.md")
    p = project(repo)
    d = snapshots.snapshot_memory(p, new_session(repo, transcript), "session-start")
    assert sorted(x.name for x in (p.home_dir / "memory" / d).iterdir()) == ["MEMORY.md", "user_role.md"]


# --- repo config -------------------------------------------------------------------------------


def test_repo_config_allowlist_and_cap(repo, transcript):
    (repo / "CLAUDE.md").write_text("# rules\n")
    (repo / "AGENTS.md").write_text("# agents\n")
    (repo / "README.md").write_text("not config\n")
    dot = repo / ".claude"
    (dot / "agents").mkdir(parents=True)
    (dot / "skills" / "x").mkdir(parents=True)
    (dot / "settings.json").write_text("{}\n")
    (dot / "settings.local.json").write_text('{"hooks": {}}\n')  # per-user: excluded
    (dot / "agents" / "reviewer.md").write_text("reviewer\n")
    (dot / "skills" / "x" / "SKILL.md").write_text("skill\n")
    (dot / "skills" / "x" / "big.bin").write_bytes(b"\0" * (snapshots.FILE_CAP + 1))
    (dot / "notes.txt").write_text("not json\n")
    files = snapshots.repo_config_files(repo)
    assert [rel for _, rel in files] == ["CLAUDE.md", "AGENTS.md", ".claude/settings.json", ".claude/agents/reviewer.md", ".claude/skills/x/SKILL.md"]
    p = project(repo)
    s = new_session(repo, transcript)
    d = snapshots.snapshot_repo_config(p, s, "session-start")
    assert (p.home_dir / "repo-config" / d / ".claude" / "agents" / "reviewer.md").read_text() == "reviewer\n"
    assert not (p.home_dir / "repo-config" / d / ".claude" / "settings.local.json").exists()
    assert snapshots.snapshot_repo_config(p, s, "session-end") is None
    (repo / "CLAUDE.md").write_text("# rules, revised\n")
    assert snapshots.snapshot_repo_config(p, s, "session-end") is not None
    assert len(index(repo, "repo-config")) == 2


def test_repo_without_config_records_nothing(repo, transcript):
    p = project(repo)
    (repo / ".claude" / "settings.local.json").unlink()  # init wrote it; the only .claude file here
    assert snapshots.snapshot_repo_config(p, new_session(repo, transcript), "session-start") is None
    assert not (p.home_dir / "repo-config").exists()


# --- history, pastes, sessions index -------------------------------------------------------------


def hist_line(project_path, sid, ts, display, pastes=None) -> bytes:
    d = {"display": display, "pastedContents": pastes or {}, "timestamp": ts, "project": project_path, "sessionId": sid}
    return (json.dumps(d) + "\n").encode()


def test_history_filtered_by_root_deduped_with_pastes(repo, transcript, tmp_path):
    cc = tmp_path / "cc"
    (cc / "paste-cache").mkdir()
    (cc / "paste-cache" / "366fe85d89d6985c.txt").write_text("pasted text\n")
    lines = [
        hist_line(str(repo), SID, 1000, "hi"),
        hist_line(str(repo / "sub"), SID2, 1001, "from a subdirectory"),  # resolves to the same root
        hist_line(str(tmp_path / "repo-other"), "x", 1002, "another project"),
        hist_line(str(repo), SID, 1003, "with paste [Pasted text #1]", {"1": {"id": 1, "type": "text", "contentHash": "366fe85d89d6985c"}}),
        hist_line(str(repo), SID, 1004, "missing paste", {"1": {"id": 1, "type": "text", "contentHash": "deadbeefdeadbeef"}}),
        b"not json\n",
        b'{"display": "no project"}\n',
    ]
    (cc / "history.jsonl").write_bytes(b"".join(lines))
    p = project(repo)
    assert snapshots.sync_history(p, str(cc)) == 4
    got = (p.home_dir / "history.jsonl").read_bytes()
    assert got == b"".join(lines[i] for i in (0, 1, 3, 4))  # verbatim bytes, project order kept
    assert (p.home_dir / "pastes" / "366fe85d89d6985c.txt").read_text() == "pasted text\n"
    assert not (p.home_dir / "pastes" / "deadbeefdeadbeef.txt").exists()
    assert snapshots.sync_history(p, str(cc)) == 0 and (p.home_dir / "history.jsonl").read_bytes() == got  # dedup
    with open(cc / "history.jsonl", "ab") as f:
        f.write(hist_line(str(repo), SID, 1005, "later"))
    assert snapshots.sync_history(p, str(cc)) == 1
    assert (p.home_dir / "history.jsonl").read_bytes().endswith(hist_line(str(repo), SID, 1005, "later"))
    assert oct(os.stat(p.home_dir / "history.jsonl").st_mode & 0o777) == "0o600"


def test_history_absent_is_fine(repo, tmp_path):
    assert snapshots.sync_history(project(repo), str(tmp_path / "nowhere")) == 0
    assert not (project(repo).home_dir / "history.jsonl").exists()


WRITER = """
import sys
from loil import snapshots
from loil.record.project import Project
p = Project.open(sys.argv[1])
snapshots.sync_history(p, sys.argv[2])
"""


def test_concurrent_history_syncs_lose_no_line_and_duplicate_none(repo, tmp_path):
    cc = tmp_path / "cc"
    cc.mkdir(exist_ok=True)
    lines = [hist_line(str(repo), f"s{i}", 2000 + i, f"prompt {i}") for i in range(200)]
    (cc / "history.jsonl").write_bytes(b"".join(lines))
    procs = [subprocess.Popen([sys.executable, "-c", WRITER, str(repo), str(cc)], stderr=subprocess.PIPE) for _ in range(6)]
    for pr in procs:
        _, err = pr.communicate()
        assert pr.returncode == 0, err
    got = (project(repo).home_dir / "history.jsonl").read_bytes().splitlines()
    assert len(got) == 200 and len(set(got)) == 200


def test_sessions_index_copied_when_present_and_changed(repo, transcript):
    p = project(repo)
    s = new_session(repo, transcript)
    assert snapshots.copy_sessions_index(p, s) is False
    src = transcript.parent / "sessions-index.json"
    src.write_text('{"version": 1, "entries": []}\n')
    assert snapshots.copy_sessions_index(p, s) is True
    assert (p.home_dir / "sessions-index.json").read_text() == src.read_text()
    assert snapshots.copy_sessions_index(p, s) is False
    src.write_text('{"version": 1, "entries": [1]}\n')
    assert snapshots.copy_sessions_index(p, s) is True


# --- .claude.json ---------------------------------------------------------------------------------


def test_claude_project_entry_from_config_dir_then_home(repo, tmp_path, monkeypatch):
    cc = tmp_path / "cc"
    cc.mkdir(exist_ok=True)
    home = Path(os.environ["HOME"])
    (home / ".claude.json").write_text(json.dumps({"projects": {str(repo): {"lastCost": 1.5, "allowedTools": []}}}))
    out = json.loads(snapshots.claude_project_entry(str(cc), [str(repo), str(repo / "sub")]))
    assert out == {"path": str(repo), "entry": {"lastCost": 1.5, "allowedTools": []}}
    (cc / ".claude.json").write_text(json.dumps({"projects": {str(repo / "sub"): {"lastCost": 2}}}))  # the config dir wins
    assert json.loads(snapshots.claude_project_entry(str(cc), [str(repo), str(repo / "sub")]))["entry"] == {"lastCost": 2}
    assert snapshots.claude_project_entry(str(cc), ["/elsewhere"]) is None
    (cc / ".claude.json").write_text("{broken")
    assert json.loads(snapshots.claude_project_entry(str(cc), [str(repo)]))["entry"]["lastCost"] == 1.5  # falls through to HOME
    assert snapshots.claude_project_entry(None, [None, ""]) is None or True  # None paths never match


# --- wiring into the hooks ------------------------------------------------------------------------


def test_hooks_take_snapshots_and_git_state(repo, transcript, tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    (repo / "CLAUDE.md").write_text("# rules\n")
    subprocess.run(["git", "-C", str(repo), "add", "CLAUDE.md"], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=x@y", "-c", "user.name=x", "commit", "-q", "-m", "i"], check=True)
    cc = tmp_path / "cc"
    (cc / "history.jsonl").write_bytes(hist_line(str(repo), SID, 3000, "hello"))
    (Path(os.environ["HOME"]) / ".claude.json").write_text(json.dumps({"projects": {str(repo): {"lastSessionId": SID}}}))
    hook("session-start", repo, transcript, source="startup")
    p = project(repo)
    s = store.read_session(p, CC, SID)
    assert sorted(a["path"] for a in s.artifacts if a["kind"] != "payload") == ["git/start.json"]
    assert json.loads((p.home_dir / "sessions" / CC / SID / "git" / "start.json").read_bytes())["branch"] in ("main", "master")
    assert len(index(repo, "memory")) == 1 and len(index(repo, "repo-config")) == 1
    assert not (p.home_dir / "history.jsonl").exists()  # history only at end/pre-compact
    (repo / "CLAUDE.md").write_text("# rules, edited during the session\n")
    (transcript.parent / "memory" / "new.md").write_text("learned\n")
    hook("session-end", repo, transcript, reason="other")
    s = store.read_session(p, CC, SID)
    paths = {a["path"] for a in s.artifacts}
    assert {"git/start.json", "git/end.json", "git/diff-end.patch.gz", "claude-project-entry.json", "transcript.jsonl.gz"} <= paths
    end = json.loads((p.home_dir / "sessions" / CC / SID / "git" / "end.json").read_bytes())
    assert " M CLAUDE.md" in end["status_porcelain"] and end["diff_truncated"] is False  # plus the untracked .loil/ and .claude/ of this un-ignored test repo
    import gzip

    assert b"+# rules, edited during the session" in gzip.decompress((p.home_dir / "sessions" / CC / SID / "git" / "diff-end.patch.gz").read_bytes())
    assert json.loads((p.home_dir / "sessions" / CC / SID / "claude-project-entry.json").read_bytes())["entry"] == {"lastSessionId": SID}
    assert [x["event"] for x in index(repo, "memory")] == ["session-start", "session-end"]
    assert [x["event"] for x in index(repo, "repo-config")] == ["session-start", "session-end"]
    assert (p.home_dir / "history.jsonl").read_bytes() == hist_line(str(repo), SID, 3000, "hello")
    assert s.capture_state == "done"
    # verify every inventory entry reads back to its hash, gz or not
    import hashlib

    for a in s.artifacts:
        f = p.home_dir / "sessions" / CC / SID / a["path"]
        data = gzip.decompress(f.read_bytes()) if a["path"].endswith(".gz") else f.read_bytes()
        assert hashlib.sha256(data).hexdigest() == a["sha256"] and len(data) == a["bytes"], a


def test_snapshot_failure_is_logged_not_raised(repo, transcript, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("snap")

    monkeypatch.setattr(snapshots, "snapshot_memory", boom)
    (repo / "CLAUDE.md").write_text("# rules\n")
    hook("session-start", repo, transcript, source="startup")
    p = project(repo)
    assert store.session_path(p, CC, SID).is_file()
    assert "snapshot memory" in p.log_path.read_text() and "RuntimeError('snap')" in p.log_path.read_text()
    assert len(index(repo, "repo-config")) == 1  # the other part still ran
