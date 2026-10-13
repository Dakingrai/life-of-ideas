"""04c: `Adapter.discover(root)` for claude-code over a fixture config dir."""

import os
import subprocess
import sys

import pytest

from loi import adapters
from loi.adapters.claude_code import _slug
from test_capture import FOREIGN, SID, SID2, env, make_layout, repo  # noqa: F401 — fixtures

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only")
CC = "claude-code"


def test_discover_confirms_by_cwd_and_skips_foreign_and_agent_files(repo, tmp_path):
    t = make_layout(tmp_path / "cc", repo, tmp_path / "repo-other")
    found = adapters.get(CC).discover(repo)
    assert [d.id for d in found] == [SID2, SID]  # oldest start first
    assert found[1].transcript_path == str(t)
    assert (found[1].start, found[1].end) == ("2026-08-26T04:44:26.420Z", "2026-08-26T05:00:00.000Z")
    assert (found[0].start, found[0].end) == ("2026-08-21T10:00:00.000Z", "2026-08-21T10:05:00.000Z")
    assert FOREIGN not in {d.id for d in found}
    assert adapters.get(CC).discover(tmp_path / "repo-other") and [d.id for d in adapters.get(CC).discover(tmp_path / "repo-other")] == [FOREIGN]


def test_discover_uses_claude_config_dir_then_home(repo, tmp_path, monkeypatch):
    make_layout(tmp_path / "cc", repo, tmp_path / "repo-other")
    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    assert adapters.get(CC).discover(repo) == []  # ~/.claude (HOME=tmp) has nothing
    make_layout(tmp_path / "home" / ".claude", repo, tmp_path / "repo-other")
    assert [d.id for d in adapters.get(CC).discover(repo)] == [SID2, SID]
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "nowhere"))
    assert adapters.get(CC).discover(repo) == []


def test_discover_transcript_without_cwd_or_timestamps(repo, tmp_path):
    make_layout(tmp_path / "cc", repo, tmp_path / "repo-other")
    pdir = tmp_path / "cc" / "projects" / _slug(str(repo))
    (pdir / "aaaaaaaa-0000-0000-0000-000000000003.jsonl").write_text('{"type":"user","cwd":"%s"}\n' % repo)
    (pdir / "bbbbbbbb-0000-0000-0000-000000000004.jsonl").write_text('{"type":"queue-operation"}\nnot json\n')
    (pdir / "bad id.jsonl").write_text("{}\n")
    ids = {d.id: d for d in adapters.get(CC).discover(repo)}
    assert "aaaaaaaa-0000-0000-0000-000000000003" in ids and ids["aaaaaaaa-0000-0000-0000-000000000003"].start is None
    assert "bbbbbbbb-0000-0000-0000-000000000004" not in ids  # no cwd: never confirmed


def test_discover_skips_a_nested_project(repo, tmp_path):
    make_layout(tmp_path / "cc", repo, tmp_path / "repo-other")
    inner = repo / "inner"
    (inner / ".loi").mkdir(parents=True)
    (inner / ".loi" / "config.toml").write_text('schema = 1\nid = "01BX5ZZKBKACTAV9WEVGEMMVRZ"\nname = "inner"\ncreated = "c"\nloi_version = "v"\n')
    pdir = tmp_path / "cc" / "projects" / _slug(str(inner))  # shares the parent's slug prefix: scanned
    pdir.mkdir(parents=True)
    nested = "cccccccc-0000-0000-0000-000000000005"
    (pdir / f"{nested}.jsonl").write_text('{"type":"user","cwd":"%s","timestamp":"2026-08-27T00:00:00.000Z"}\n' % inner)
    assert nested not in {d.id for d in adapters.get(CC).discover(repo)}  # its nearest .loi is inner
    assert [d.id for d in adapters.get(CC).discover(inner)] == [nested]
    # a transcript whose cwd is a *parent* of the root belongs to no project of ours
    (pdir / "dddddddd-0000-0000-0000-000000000006.jsonl").write_text('{"type":"user","cwd":"%s"}\n' % tmp_path)
    assert "dddddddd-0000-0000-0000-000000000006" not in {d.id for d in adapters.get(CC).discover(repo)}
    assert "dddddddd-0000-0000-0000-000000000006" not in {d.id for d in adapters.get(CC).discover(inner)}


def test_slug_matches_claude_codes_project_dir_names():
    assert _slug("/scratch/drai2/dream-project/life-of-ideas") == "-scratch-drai2-dream-project-life-of-ideas"
    assert _slug("/home/u/my_repo.v2") == "-home-u-my-repo-v2"
