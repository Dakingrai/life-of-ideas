import os

import pytest

from loi.util import gitignore as gi


def test_absent(tmp_path):
    assert gi.ensure_line(tmp_path, ".loi/") is True
    assert (tmp_path / ".gitignore").read_bytes() == b".loi/\n"
    assert gi.has_line(tmp_path, ".loi/")
    assert not gi.has_line(tmp_path / "nowhere", ".loi/")


def test_no_trailing_newline(tmp_path):
    (tmp_path / ".gitignore").write_bytes(b".loi/")
    assert gi.ensure_line(tmp_path, ".loi/local.toml")
    assert (tmp_path / ".gitignore").read_bytes() == b".loi/\n.loi/local.toml\n"


def test_last_line_is_target_without_newline(tmp_path):
    (tmp_path / ".gitignore").write_bytes(b"x\n.loi/")
    assert gi.has_line(tmp_path, ".loi/")
    assert gi.ensure_line(tmp_path, ".loi/") is False


def test_crlf(tmp_path):
    f = tmp_path / ".gitignore"
    f.write_bytes(b"a\r\n.loi/\r\nb\r\n")
    assert gi.has_line(tmp_path, ".loi/")
    assert gi.ensure_line(tmp_path, ".loi/") is False
    assert gi.ensure_line(tmp_path, "c")
    assert f.read_bytes() == b"a\r\n.loi/\r\nb\r\nc\n"
    assert gi.remove_line(tmp_path, ".loi/")
    assert f.read_bytes() == b"a\r\nb\r\nc\n"


def test_duplicate(tmp_path):
    f = tmp_path / ".gitignore"
    f.write_bytes(b"x\n.loi/\n")
    assert gi.ensure_line(tmp_path, ".loi/") is False
    assert f.read_bytes() == b"x\n.loi/\n"


def test_bom(tmp_path):
    f = tmp_path / ".gitignore"
    f.write_bytes(b"\xef\xbb\xbf.loi/\n")
    assert gi.has_line(tmp_path, ".loi/")
    assert gi.ensure_line(tmp_path, ".loi/") is False
    assert f.read_bytes() == b"\xef\xbb\xbf.loi/\n"


def test_negation_is_not_a_match(tmp_path):
    (tmp_path / ".gitignore").write_bytes(b"!.loi/\n")
    assert not gi.has_line(tmp_path, ".loi/")
    assert gi.ensure_line(tmp_path, ".loi/")


def test_remove(tmp_path):
    f = tmp_path / ".gitignore"
    f.write_bytes(b"x\n.loi/\ny\n")
    assert gi.remove_line(tmp_path, ".loi/")
    assert f.read_bytes() == b"x\ny\n"
    assert gi.remove_line(tmp_path, ".loi/") is False
    assert gi.remove_line(tmp_path / "nowhere", "z") is False


def test_remove_leaving_empty(tmp_path):
    f = tmp_path / ".gitignore"
    f.write_bytes(b".loi/\n")
    assert gi.remove_line(tmp_path, ".loi/")
    assert f.read_bytes() == b""


def test_non_utf8_preserved(tmp_path):
    f = tmp_path / ".gitignore"
    f.write_bytes(b"caf\xe9\n.loi/\n")
    assert gi.ensure_line(tmp_path, "z")
    assert f.read_bytes() == b"caf\xe9\n.loi/\nz\n"
    assert gi.remove_line(tmp_path, ".loi/")
    assert f.read_bytes() == b"caf\xe9\nz\n"


def test_is_git_checkout(tmp_path):
    if any((a / ".git").exists() for a in tmp_path.parents):
        import pytest

        pytest.skip(f"an ancestor of {tmp_path} is a git checkout")
    d = tmp_path / "d"
    d.mkdir()
    assert not gi.is_git_checkout(d)
    (d / ".git").mkdir()  # an empty .git/: git itself says "not a git repository"
    assert not gi.is_git_checkout(d)
    (d / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    assert gi.is_git_checkout(d)
    assert gi.is_git_checkout(d / "sub" / "deeper")  # ancestors count; the path need not exist
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text("gitdir: /elsewhere\n")  # a worktree or submodule: a file
    assert gi.is_git_checkout(wt)
    assert not gi.is_git_checkout(tmp_path)


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads everything")
def test_is_git_checkout_unreadable_git_dir_is_not_a_checkout(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    proj = tmp_path / "sub" / "proj"
    proj.mkdir(parents=True)
    assert gi.is_git_checkout(proj)
    (tmp_path / ".git").chmod(0)
    try:
        assert gi.is_git_checkout(proj) is False  # never raises into a hook
    finally:
        (tmp_path / ".git").chmod(0o700)


def test_trailing_blanks_match_like_git(tmp_path):
    (tmp_path / ".gitignore").write_text(".loi/record/ \nkeep\n")
    assert gi.has_line(tmp_path, ".loi/record/")
    assert gi.remove_line(tmp_path, ".loi/record/") and (tmp_path / ".gitignore").read_text() == "keep\n"


def test_unreadable_gitignore_is_a_loi_error(tmp_path):
    from loi.errors import LoiError

    (tmp_path / ".gitignore").mkdir()
    with pytest.raises(LoiError, match="cannot read"):
        gi.has_line(tmp_path, "x")
