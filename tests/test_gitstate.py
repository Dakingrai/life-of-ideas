"""`gitstate`: read-only git snapshots that never write into the repo and never raise."""

import gzip
import json
import os
import subprocess
import sys

import pytest

from loi import gitstate

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only")


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), "-c", "user.email=x@y", "-c", "user.name=x", *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for var in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        monkeypatch.delenv(var, raising=False)
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q")
    for i in range(50):
        (r / f"f{i:02d}.txt").write_text(f"file {i}\n")
    git(r, "add", ".")
    git(r, "commit", "-q", "-m", "one")
    return r


def test_start_has_head_and_branch(repo):
    d = json.loads(gitstate.start_json(repo))
    assert set(d) == {"at", "head", "branch"}
    assert d["head"] == git(repo, "rev-parse", "HEAD").stdout.decode().strip() and len(d["head"]) == 40
    assert d["branch"] in ("main", "master")


def test_end_has_status_and_diff_and_never_touches_the_index(repo):
    (repo / "f00.txt").write_text("file 0 changed\n")
    (repo / "new.txt").write_text("untracked\n")
    index = repo / ".git" / "index"
    os.utime(index, (1_000_000_000, 1_000_000_000))  # any refresh by `git status` would move this
    before = index.stat().st_mtime_ns
    end, diff = gitstate.end_json_and_diff(repo)
    d = json.loads(end)
    assert d["status_porcelain"] == [" M f00.txt", "?? new.txt"]
    assert d["diff_truncated"] is False and d["diff_bytes"] == len(diff)
    assert b"-file 0\n+file 0 changed\n" in diff and b"new.txt" not in diff  # untracked files are in the status, not the diff
    assert index.stat().st_mtime_ns == before  # GIT_OPTIONAL_LOCKS=0: no write into the repo
    # a clean tree: no diff bytes at all
    (repo / "f00.txt").write_text("file 0\n")
    (repo / "new.txt").unlink()
    end, diff = gitstate.end_json_and_diff(repo)
    assert diff is None and json.loads(end)["status_porcelain"] == [] and json.loads(end)["diff_bytes"] == 0  # 0: clean; null: git could not run


def test_diff_is_capped_and_marked(repo):
    (repo / "f01.txt").write_text("x\n" * 5000)
    end, diff = gitstate.end_json_and_diff(repo, cap=1000)
    d = json.loads(end)
    assert d["diff_truncated"] is True and len(diff) == 1000 and d["diff_bytes"] == 1000


def test_unborn_branch_and_detached_head(repo, tmp_path):
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    git(fresh, "init", "-q")
    (fresh / "a.txt").write_text("a\n")
    d = json.loads(gitstate.start_json(fresh))
    assert d["head"] is None  # no commit yet
    end, diff = gitstate.end_json_and_diff(fresh)
    assert json.loads(end)["status_porcelain"] == ["?? a.txt"] and diff is None
    sha = json.loads(gitstate.start_json(repo))["head"]
    git(repo, "checkout", "-q", "--detach", sha)
    assert json.loads(gitstate.start_json(repo))["branch"] == "HEAD"


def test_not_a_checkout_costs_nothing_and_gives_none(tmp_path):
    d = tmp_path / "plain"
    d.mkdir()
    assert gitstate.start_json(d) is None
    assert gitstate.end_json_and_diff(d) == (None, None)


def test_git_missing_or_broken_gives_null_fields(repo, monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path / "nowhere"))
    d = json.loads(gitstate.start_json(repo))  # still a checkout on disk: the file exists, the fields are null
    assert d["head"] is None and d["branch"] is None
    end, diff = gitstate.end_json_and_diff(repo)
    assert json.loads(end)["status_porcelain"] is None and diff is None


def test_env_git_dir_does_not_redirect(repo, tmp_path, monkeypatch):
    other = tmp_path / "other"
    other.mkdir()
    git(other, "init", "-q")
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    assert json.loads(gitstate.start_json(repo))["head"] is not None  # repo's HEAD, not `other`'s unborn one


def test_diff_bytes_round_trip_through_gzip_destination():
    data = b"--- a\n+++ b\n"
    assert gzip.decompress(gzip.compress(data)) == data  # the capture layer gzips `git/diff-end.patch.gz`
