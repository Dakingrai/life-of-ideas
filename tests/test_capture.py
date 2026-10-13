"""`capture()` on a synthetic copy of the real Claude Code layout (small files, the real
names: `<S>.jsonl`, `<S>/{subagents/agent-*.jsonl|.meta.json, tool-results/*.txt, …}`,
`file-history/<S>/<16hex>@vN`, plans named by `planFilePath`), plus the hook wiring."""

import gzip
import hashlib
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from loil import adapters
from loil.record import store
from loil.adapters.claude_code import PlanScan, _plans_dir, _slug
from loil.capture import capture
from loil.commands.init import run_init
from loil.record.project import Project

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only")
CC = "claude-code"
SID = "eb8fb6e3-1f80-4688-a4c5-f7fc4d1e4991"
SID2 = "eeeeeeee-0000-0000-0000-000000000002"
FOREIGN = "ffffffff-0000-0000-0000-000000000001"
PY = [sys.executable, "-m", "loil"]

ADAPTER_PATHS = [  # what one capture of SID's layout stores, besides plans
    "transcript.jsonl.gz",
    "custom-title.json",
    "subagents/agent-a1f227a791f782d80.jsonl.gz",
    "subagents/agent-a1f227a791f782d80.meta.json",
    "subagents/agent-a5ed6d20facc4d2df.jsonl.gz",
    "subagents/agent-a5ed6d20facc4d2df.meta.json",
    "tool-results/b0fxvb6p5.txt.gz",
    "tool-results/toolu_01YZufg1TR8Wdo4XQzaJ6mfX.txt.gz",
    "tool-results/deep/too-deep.txt.gz",
    "file-history/08b710c6e932d468@v1.gz",
    "file-history/08b710c6e932d468@v2.gz",
]
N_ARTIFACTS = len(ADAPTER_PATHS) + 3  # + three plans


def line(**d) -> bytes:
    return (json.dumps(d) + "\n").encode()


def make_layout(cc: Path, repo: Path, other_repo: Path, *, big: int = 0) -> Path:
    """`cc` = a Claude config dir. Returns the transcript path of `SID`."""
    plans = cc / "plans"
    plans.mkdir(parents=True, exist_ok=True)
    (plans / "a.md").write_text("# plan a\n")
    (plans / "b.md").write_text("# plan b\n")
    (plans / "c.md").write_text("# plan c (a subagent's, named only by a tool call)\n")
    pdir = cc / "projects" / _slug(str(repo))
    pdir.mkdir(parents=True, exist_ok=True)
    t = pdir / f"{SID}.jsonl"
    body = [
        line(type="queue-operation", operation="enqueue", sessionId=SID),  # no timestamp, no cwd
        line(type="user", cwd=str(repo), sessionId=SID, timestamp="2026-08-26T04:44:26.420Z", message={"role": "user", "content": "hi"}),
        line(type="ai-title", aiTitle="first title", sessionId=SID),
        line(type="attachment", timestamp="2026-08-26T04:45:00.000Z", attachment={"type": "plan_mode", "planFilePath": str(plans / "a.md")}),
        line(type="assistant", timestamp="2026-08-26T04:46:00.000Z", message={"content": [{"type": "tool_use", "input": {"file_path": str(plans / "c.md")}}]}),
        line(type="attachment", timestamp="2026-08-26T04:47:00.000Z", attachment={"type": "plan_file_reference", "planFilePath": str(plans / "b.md")}),
        line(type="attachment", timestamp="2026-08-26T04:47:30.000Z", attachment={"type": "plan_mode_exit", "planFilePath": str(plans / "a.md")}),
        line(type="user", timestamp="2026-08-26T04:48:00.000Z", message={"content": 'text with \\"planFilePath\\" escaped inside a string and an "aiTitle" word'}),
        line(type="ai-title", aiTitle="  plan the capture  ", sessionId=SID),
        line(type="assistant", timestamp="2026-08-26T05:00:00.000Z", message={"content": "x" * big}),
    ]
    t.write_bytes(b"".join(body))
    t.chmod(0o600)
    s = pdir / SID
    (s / "subagents").mkdir(parents=True)
    (s / "tool-results").mkdir()
    (s / "custom-title.json").write_text('{"customTitle":"plan 04c"}\n')
    for aid in ("a1f227a791f782d80", "a5ed6d20facc4d2df"):
        (s / "subagents" / f"agent-{aid}.jsonl").write_bytes(line(agentId=aid, sessionId=SID, isSidechain=True, cwd=str(repo), timestamp="2026-08-26T04:50:00.000Z") * 3)
        (s / "subagents" / f"agent-{aid}.meta.json").write_text(json.dumps({"agentType": "Explore", "description": "d", "spawnDepth": 1, "toolUseId": "toolu_x"}))
    (s / "tool-results" / "b0fxvb6p5.txt").write_bytes(b"result one\n" * 200)
    (s / "tool-results" / "toolu_01YZufg1TR8Wdo4XQzaJ6mfX.txt").write_bytes(b"result two\n" * 300)
    (s / "tool-results" / "deep").mkdir()
    (s / "tool-results" / "deep" / "too-deep.txt").write_text("depth 3: captured too\n")
    os.symlink(s / "custom-title.json", s / "link.json")  # symlink: skipped
    os.symlink(s / "tool-results", s / "linked-dir")  # symlinked dir: never followed
    (s / "git").mkdir()
    (s / "git" / "x").write_text("a Claude-side file under a name loil owns: skipped\n")
    fh = cc / "file-history" / SID
    fh.mkdir(parents=True)
    (fh / "08b710c6e932d468@v1").write_bytes(b"")  # Claude writes empty first versions
    (fh / "08b710c6e932d468@v2").write_text("# Literature-mapping harness\n")
    # a second session of the same project, started in a subdirectory; a top-level agent file
    (pdir / f"{SID2}.jsonl").write_bytes(
        line(type="user", cwd=str(repo / "sub"), sessionId=SID2, timestamp="2026-08-21T10:00:00.000Z")
        + line(type="assistant", timestamp="2026-08-21T10:05:00.000Z")
    )
    (pdir / "agent-a1.jsonl").write_bytes(line(type="user", cwd=str(repo), agentId="a1", timestamp="2026-08-21T10:00:00.000Z"))
    # a same-prefix foreign project dir
    fdir = cc / "projects" / _slug(str(other_repo))
    assert fdir.name.startswith(_slug(str(repo)))  # same prefix: the trap `discover` must not fall into
    fdir.mkdir()
    (fdir / f"{FOREIGN}.jsonl").write_bytes(
        line(type="user", cwd=str(other_repo), sessionId=FOREIGN, timestamp="2026-08-20T10:00:00.000Z")
        + line(type="assistant", timestamp="2026-08-20T10:01:00.000Z")
    )
    return t


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
    fake_bin = tmp_path / "fakebin"  # a `claude` on PATH: init enables the adapter
    fake_bin.mkdir()
    (fake_bin / "claude").write_text("#!/bin/sh\nexit 0\n")
    (fake_bin / "claude").chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}:/usr/bin:/bin")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cc"))
    return home


def _folder(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)  # no `git init`: the root is the nearest .loil/
    return path


@pytest.fixture
def repo(tmp_path, env):
    r = _folder(tmp_path / "repo")
    (r / "sub").mkdir()
    other = _folder(tmp_path / "repo-other")  # a sibling whose slug shares the prefix…
    (other / ".loil").mkdir()  # …and is a project of its own
    (other / ".loil" / "config.toml").write_text('schema = 1\nid = "01BX5ZZKBKACTAV9WEVGEMMVRZ"\nname = "other"\ncreated = "c"\nloil_version = "v"\n')
    assert run_init(cwd=r, backfill=False, out=io.StringIO(), err=io.StringIO()) == 0
    return r


@pytest.fixture
def transcript(tmp_path, repo):
    return make_layout(tmp_path / "cc", repo, tmp_path / "repo-other")


def project(repo) -> Project:
    return Project.open(repo)


def new_session(repo, transcript, sid=SID, start="2026-08-26T04:44:26.420Z", **kw) -> store.Session:
    p = project(repo)
    s = store.Session(id=sid, agent=CC, project=str(repo), transcript_path=str(transcript) if transcript else None, start=start, **kw)
    store.update_session(p, CC, sid, lambda s: None, default=s)
    return s


def do_capture(repo, s, rows=None):
    return capture(project(repo), s, adapters.get(CC), rows=rows)


def adir(repo, sid=SID) -> Path:
    return project(repo).artifacts_dir(CC, sid)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_back(path: Path) -> bytes:
    return gzip.decompress(path.read_bytes()) if path.suffix == ".gz" else path.read_bytes()


def modes(root: Path):
    files, dirs = set(), set()
    for dp, dns, fns in os.walk(root):
        dirs.add(oct(os.stat(dp).st_mode & 0o777))
        for n in fns:
            files.add(oct(os.stat(os.path.join(dp, n)).st_mode & 0o777))
    return files, dirs


def non_payload(s):
    return [a for a in s.artifacts if a["kind"] != "payload"]


# --- the artifacts one capture produces -------------------------------------------------------


def test_capture_copies_every_artifact_with_source_hashes(repo, transcript):
    s = new_session(repo, transcript)
    rows = []
    s = do_capture(repo, s, rows)
    assert s.capture_state == "done" and s.capture_error is None
    by_path = {a["path"]: a for a in s.artifacts}
    plans = sorted(p for p in by_path if p.startswith("plans/"))
    assert sorted(by_path) == sorted([*ADAPTER_PATHS, *plans])
    assert len(plans) == 3  # a.md and b.md via planFilePath; c.md only as a tool-call path string
    assert {a["kind"] for a in s.artifacts} == {"transcript", "other", "subagent", "tool-result", "plan", "file-history"}
    assert by_path["custom-title.json"]["kind"] == "other"
    assert by_path["subagents/agent-a1f227a791f782d80.meta.json"]["kind"] == "subagent"
    assert by_path["tool-results/b0fxvb6p5.txt.gz"]["kind"] == "tool-result"
    assert by_path["tool-results/deep/too-deep.txt.gz"]["kind"] == "tool-result"  # recursive now
    assert by_path["file-history/08b710c6e932d468@v1.gz"]["bytes"] == 0
    assert s.title == "plan the capture"  # the last ai-title line, stripped
    copied = [r for r in rows if r[2] == "copied"]
    assert len(copied) == len(s.artifacts) == N_ARTIFACTS
    assert ("other", "git/x", "skipped") in rows  # a Claude-side file under a reserved name
    assert "git/x is reserved" not in project(repo).log_path.read_text() and "git is reserved" in project(repo).log_path.read_text()
    assert not (adir(repo) / "git").exists() and not (adir(repo) / "linked-dir").exists() and not (adir(repo) / "link.json").exists()
    # bytes/sha256 describe the *source* stream; the copy reads back identical
    target = adir(repo)
    for a in s.artifacts:
        data = read_back(target / a["path"])
        assert a["bytes"] == len(data) and a["sha256"] == hashlib.sha256(data).hexdigest(), a
    assert read_back(target / "transcript.jsonl.gz") == transcript.read_bytes()
    assert read_back(target / "file-history/08b710c6e932d468@v2.gz") == b"# Literature-mapping harness\n"
    plan_contents = sorted(read_back(target / p) for p in plans)
    assert plan_contents == [b"# plan a\n", b"# plan b\n", b"# plan c (a subagent's, named only by a tool call)\n"]
    for p in plans:
        assert p == f"plans/{sha(target / p)[:12]}.md"
    assert modes(target) == ({"0o600"}, {"0o700"})
    assert oct(os.stat(target.parent).st_mode & 0o777) == "0o700"  # sessions/claude-code/
    # gzip with mtime=0: the header carries no timestamp, so identical input → identical bytes
    assert (target / "transcript.jsonl.gz").read_bytes()[4:8] == b"\0\0\0\0"
    assert not any(n.startswith(".") for _, _, fns in os.walk(target) for n in fns)  # no temp files left


def test_custom_title_wins_over_ai_title(repo, transcript):
    with open(transcript, "ab") as f:
        f.write(line(type="custom-title", customTitle="my name", sessionId=SID))
        f.write(line(type="ai-title", aiTitle="later ai title", sessionId=SID))
    s = do_capture(repo, new_session(repo, transcript))
    assert s.title == "my name"


def test_second_capture_is_all_unchanged_and_idempotent(repo, transcript):
    s = do_capture(repo, new_session(repo, transcript))
    target = adir(repo)
    before = {str(p): p.stat().st_mtime_ns for p in target.rglob("*") if p.is_file() and p.name != "session.json"}
    rows = []
    s2 = do_capture(repo, s, rows)
    statuses = {r[2] for r in rows}
    assert statuses == {"unchanged", "skipped"} and len([r for r in rows if r[2] == "unchanged"]) == len(s.artifacts)
    assert s2.artifacts == s.artifacts and s2.capture_state == "done"
    assert {str(p): p.stat().st_mtime_ns for p in target.rglob("*") if p.is_file() and p.name != "session.json"} == before


def test_growing_transcript_recopies_only_the_transcript(repo, transcript):
    s = do_capture(repo, new_session(repo, transcript))
    with open(transcript, "ab") as f:
        f.write(line(type="user", timestamp="2026-08-26T06:00:00.000Z", message="more"))
    rows = []
    s = do_capture(repo, s, rows)
    assert [r[1] for r in rows if r[2] == "copied"] == ["transcript.jsonl.gz"]
    t = next(a for a in s.artifacts if a["path"] == "transcript.jsonl.gz")
    assert t["bytes"] == transcript.stat().st_size and t["sha256"] == sha(transcript)


def test_same_size_different_content_is_recopied(repo, transcript):
    s = do_capture(repo, new_session(repo, transcript))
    title = transcript.parent / SID / "custom-title.json"
    title.write_text('{"customTitle":"plan 04d"}\n')  # same length, other bytes
    rows = []
    do_capture(repo, s, rows)
    assert [r[1] for r in rows if r[2] == "copied"] == ["custom-title.json"]


def test_wiped_store_recopies_everything(repo, transcript):
    import shutil

    s = do_capture(repo, new_session(repo, transcript))
    for x in adir(repo).iterdir():  # the captures go; the session file beside them stays
        shutil.rmtree(x) if x.is_dir() else (x.unlink() if x.name != "session.json" else None)
    rows = []
    s2 = do_capture(repo, s, rows)
    assert {r[2] for r in rows if r[1] != "git/x"} == {"copied"} and s2.artifacts == s.artifacts


def test_wiped_session_dir_takes_the_session_with_it(repo, transcript):
    """The session file lives beside its captures, so wiping the dir is wiping the session."""
    import shutil

    from loil.errors import LoilError

    s = do_capture(repo, new_session(repo, transcript))
    shutil.rmtree(adir(repo))
    with pytest.raises(LoilError, match="no session"):
        do_capture(repo, s)
    assert not adir(repo).exists()  # nothing recreated


def test_sources_under_reserved_names_are_skipped(repo, transcript):
    """`session.json`, `payloads/`, `plans/`, … are loil's: a Claude-side file there must not
    overwrite ours."""
    from loil.adapters.claude_code import tree_sources

    sdir = transcript.parent / SID
    (sdir / "session.json").write_text('{"not": "ours"}\n')
    (sdir / "payloads").mkdir()
    (sdir / "payloads" / "fake.json").write_text("{}\n")
    assert ("other", str(sdir / "session.json"), "session.json") in list(tree_sources(str(sdir)))  # the adapter does yield it
    rows = []
    s = do_capture(repo, new_session(repo, transcript), rows)
    assert s.capture_state == "done" and s.capture_error is None
    skipped = sorted(r[1] for r in rows if r[2] == "skipped")
    assert skipped == ["git/x", "payloads/fake.json", "session.json"]
    assert not {"session.json", "payloads/fake.json"} & {a["path"] for a in s.artifacts}
    assert store.read_session(project(repo), CC, SID) == s  # still ours, still valid
    assert "session.json is reserved" in project(repo).log_path.read_text()
    assert not (adir(repo) / "payloads").exists()


def test_oversized_source_is_skipped_not_fatal(repo, transcript, monkeypatch):
    import loil.capture as cap

    monkeypatch.setattr(cap, "MAX_BYTES", 2500)  # the 3000-byte tool result is over the cap
    rows = []
    s = do_capture(repo, new_session(repo, transcript), rows)
    assert s.capture_state == "done"
    assert ("tool-result", "tool-results/toolu_01YZufg1TR8Wdo4XQzaJ6mfX.txt.gz", "skipped") in rows
    assert "over the 0 MB cap" in project(repo).log_path.read_text()
    assert "tool-results/toolu_01YZufg1TR8Wdo4XQzaJ6mfX.txt.gz" not in {a["path"] for a in s.artifacts}


def test_inventory_drops_gone_copies_only_after_a_successful_run(repo, transcript):
    s = do_capture(repo, new_session(repo, transcript))
    gone = "subagents/agent-a1f227a791f782d80.meta.json"
    (adir(repo) / gone).unlink()
    (transcript.parent / SID / "subagents" / "agent-a1f227a791f782d80.meta.json").unlink()  # source gone too: not re-copied
    s2 = do_capture(repo, s)
    assert s2.capture_state == "done" and gone not in {a["path"] for a in s2.artifacts}
    assert len(s2.artifacts) == N_ARTIFACTS - 1
    # a failed run keeps the inventory even when copies are missing
    other = "custom-title.json"
    (adir(repo) / other).unlink()
    p = project(repo)
    store.update_session(p, CC, SID, lambda x: setattr(x, "transcript_path", str(transcript.parent / "missing.jsonl")))
    s3 = do_capture(repo, s2)
    assert s3.capture_state == "failed" and other in {a["path"] for a in s3.artifacts}

    # a malformed entry never breaks finish
    def fix(x):
        x.transcript_path = str(transcript)
        x.artifacts.append({"kind": "other", "path": None})

    store.update_session(p, CC, SID, fix)
    s4 = do_capture(repo, s3)
    assert s4.capture_state == "done" and all(isinstance(a["path"], str) for a in s4.artifacts)


def test_plans_are_append_only_and_deleted_ones_skipped(repo, transcript, tmp_path):
    s = do_capture(repo, new_session(repo, transcript))
    first = sorted(a["path"] for a in s.artifacts if a["kind"] == "plan")
    (tmp_path / "cc" / "plans" / "a.md").write_text("# plan a, rewritten\n")
    (tmp_path / "cc" / "plans" / "b.md").unlink()
    rows = []
    s = do_capture(repo, s, rows)
    plans = sorted(a["path"] for a in s.artifacts if a["kind"] == "plan")
    assert len(plans) == 4 and set(first) < set(plans)  # the old copies stay listed and on disk
    plan_rows = [r for r in rows if r[0] == "plan"]
    assert [r for r in plan_rows if r[2] == "copied"] == [("plan", [p for p in plans if p not in first][0], "copied")]
    assert len(plan_rows) == 2  # the rewritten a (copied) and the untouched c (unchanged); b is gone
    assert all((adir(repo) / p).is_file() for p in plans)


def test_file_history_absent_is_fine(repo, transcript, tmp_path):
    import shutil

    shutil.rmtree(tmp_path / "cc" / "file-history")
    s = do_capture(repo, new_session(repo, transcript))
    assert s.capture_state == "done" and not any(a["kind"] == "file-history" for a in s.artifacts)
    assert not (adir(repo) / "file-history").exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads anything")
def test_unreadable_source_fails_with_one_line_error(repo, transcript):
    bad = transcript.parent / SID / "tool-results" / "b0fxvb6p5.txt"
    bad.chmod(0)
    try:
        s = do_capture(repo, new_session(repo, transcript))
    finally:
        bad.chmod(0o644)
    assert s.capture_state == "failed"
    assert s.capture_error and "\n" not in s.capture_error and "b0fxvb6p5.txt" in s.capture_error
    assert {a["path"] for a in s.artifacts} >= {"transcript.jsonl.gz", "custom-title.json"}  # what was copied before stays
    assert "capture claude-code/" in project(repo).log_path.read_text()


def test_missing_transcript_and_null_transcript_path_fail_clearly(repo, transcript):
    s = new_session(repo, transcript)
    transcript.unlink()
    s = do_capture(repo, s)
    assert s.capture_state == "failed" and "transcript missing" in s.capture_error and s.artifacts == []
    s = new_session(repo, None, sid=SID2)
    s = do_capture(repo, s)
    assert s.capture_state == "failed" and "no transcript_path" in s.capture_error


def test_capture_begins_by_clearing_the_state(repo, transcript, monkeypatch):
    s = do_capture(repo, new_session(repo, transcript))
    seen = []

    def sources(session):
        seen.append(store.read_session(project(repo), CC, SID).capture_state)
        raise RuntimeError("boom in sources")

    monkeypatch.setattr(adapters.get(CC), "sources", sources)
    s = do_capture(repo, s)
    assert seen == [None] and s.capture_state == "failed" and s.capture_error == "RuntimeError: boom in sources"
    assert len(s.artifacts) == N_ARTIFACTS  # untouched on failure


def test_plan_scan_finds_keys_across_chunk_boundaries_not_escaped_text(transcript, tmp_path):
    plans = str(tmp_path / "cc" / "plans")
    for chunk in (1, 7, 64, 1 << 20):
        sc = PlanScan(plans)
        data = transcript.read_bytes()
        for i in range(0, len(data), chunk):
            sc.feed(data[i : i + chunk])
        sc.feed(b"")
        assert sorted(os.path.basename(p) for p in sc.paths()) == ["a.md", "b.md"], chunk
        assert [os.path.basename(p) for p in sc.string_paths()] == ["c.md"]  # the standalone-string half of the rule
        assert sc.title == "plan the capture", chunk
    assert _plans_dir(str(transcript)) == plans


def test_data_sources_are_stored_and_idempotent(repo, transcript):
    from loil.adapters.base import Source
    from loil.capture import store_sources

    s = new_session(repo, transcript)
    rows = []
    s = store_sources(project(repo), s, [Source("payload", None, "payloads/x.json", data=b'{"a":1}'), Source("git", None, "git/start.json", data=b"{}")], rows=rows)
    assert [r[2] for r in rows] == ["copied", "copied"] and s.capture_state is None
    assert read_back(adir(repo) / "payloads" / "x.json") == b'{"a":1}'
    assert {a["path"]: a["bytes"] for a in s.artifacts} == {"payloads/x.json": 7, "git/start.json": 2}
    rows = []
    s = store_sources(project(repo), s, [Source("payload", None, "payloads/x.json", data=b'{"a":1}')], rows=rows)
    assert rows == [("payload", "payloads/x.json", "unchanged")] and len(s.artifacts) == 2
    with pytest.raises(ValueError):
        Source("payload", "/x", "y", data=b"z")  # exactly one of src and data


# --- hook wiring ---------------------------------------------------------------------------------


def hook_payload(event, repo, transcript, **over) -> bytes:
    d = {"session_id": SID, "transcript_path": str(transcript), "cwd": str(repo), "hook_event_name": event}
    d.update(over)
    return json.dumps(d).encode()


def test_session_end_hook_captures_after_end(repo, transcript):
    ad = adapters.get(CC)
    ad.handle("session-start", hook_payload("session-start", repo, transcript, source="startup"))
    assert ad.handle("session-end", hook_payload("session-end", repo, transcript, reason="other")) is None
    s = store.read_session(project(repo), CC, SID)
    assert s.end and s.end_reason == "other" and s.capture_state == "done" and len(non_payload(s)) == N_ARTIFACTS
    assert sorted(a["kind"] for a in s.artifacts).count("payload") == 2
    assert (adir(repo) / "transcript.jsonl.gz").is_file() and s.title == "plan the capture"


def test_pre_compact_hook_snapshots_without_end(repo, transcript):
    ad = adapters.get(CC)
    ad.handle("session-start", hook_payload("session-start", repo, transcript, source="startup"))
    ad.handle("pre-compact", hook_payload("pre-compact", repo, transcript, trigger="manual"))
    s = store.read_session(project(repo), CC, SID)
    assert s.end is None and s.segments[-1]["source"] == "pre-compact" and s.capture_state == "done"


def test_session_end_subprocess_wall_time_with_capture(repo, transcript, tmp_path):
    """Timing recorded: a ~1 MB synthetic layout through `python -m loil hook`."""
    big_t = make_layout(tmp_path / "cc2", repo, tmp_path / "repo-other", big=1 << 20)
    ad = adapters.get(CC)
    ad.handle("session-start", hook_payload("session-start", repo, big_t, source="startup"))
    e = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE")}
    data = hook_payload("session-end", repo, big_t, reason="other")
    times = []
    for _ in range(3):
        t0 = time.perf_counter()
        p = subprocess.run([*PY, "hook", CC, "session-end"], input=data, cwd=repo, env=e, capture_output=True)
        times.append(time.perf_counter() - t0)
        assert p.returncode == 0 and p.stdout == b"", p.stderr
    s = store.read_session(project(repo), CC, SID)
    assert s.capture_state == "done"
    print(f"\nloil hook claude-code session-end (1 MB layout): first {times[0]*1000:.0f} ms, idempotent best {min(times[1:])*1000:.0f} ms (budget 30 s)")
    assert min(times) < 5


def test_concurrent_session_end_and_pre_compact_captures_are_not_torn(repo, transcript):
    ad = adapters.get(CC)
    ad.handle("session-start", hook_payload("session-start", repo, transcript, source="startup"))
    e = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE")}
    procs = []
    for i in range(6):
        ev, over = ("pre-compact", {"trigger": "manual"}) if i % 2 else ("session-end", {"reason": "other"})
        pr = subprocess.Popen([*PY, "hook", CC, ev], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=repo, env=e)
        procs.append((pr, hook_payload(ev, repo, transcript, **over)))
    for pr, data in procs:
        out, err = pr.communicate(data)
        assert pr.returncode == 0 and out == b"", err
    s = store.read_session(project(repo), CC, SID)
    assert s.capture_state == "done" and len(non_payload(s)) == N_ARTIFACTS
    for a in s.artifacts:
        data = read_back(adir(repo) / a["path"])
        assert hashlib.sha256(data).hexdigest() == a["sha256"]
    assert not any(n.startswith(".") for _, _, fns in os.walk(adir(repo)) for n in fns)


def test_one_line_keeps_the_tail_of_long_errors():
    from loil.capture import ONE_LINE_MAX, _one_line

    path = "/" + "/".join(f"segment-{i:03d}" for i in range(40)) + "/needle.txt"
    assert len(path) > ONE_LINE_MAX
    line = _one_line(PermissionError(13, "Permission denied", path))
    assert len(line) <= ONE_LINE_MAX
    assert "\n" not in line
    assert line.startswith("PermissionError:")
    assert line.endswith("needle.txt'")
    assert " … " in line
    assert _one_line(ValueError("short  and\nmulti-line")) == "ValueError: short and multi-line"
