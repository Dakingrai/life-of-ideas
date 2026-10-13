import os

import pytest

from loil.util import gitignore as gi


def test_absent(tmp_path):
    assert gi.ensure_line(tmp_path, ".loil/") is True
    assert (tmp_path / ".gitignore").read_bytes() == b".loil/\n"
    assert gi.has_line(tmp_path, ".loil/")
    assert not gi.has_line(tmp_path / "nowhere", ".loil/")


def test_no_trailing_newline(tmp_path):
    (tmp_path / ".gitignore").write_bytes(b".loil/")
    assert gi.ensure_line(tmp_path, ".loil/local.toml")
    assert (tmp_path / ".gitignore").read_bytes() == b".loil/\n.loil/local.toml\n"


def test_last_line_is_target_without_newline(tmp_path):
    (tmp_path / ".gitignore").write_bytes(b"x\n.loil/")
    assert gi.has_line(tmp_path, ".loil/")
    assert gi.ensure_line(tmp_path, ".loil/") is False


def test_crlf(tmp_path):
    f = tmp_path / ".gitignore"
    f.write_bytes(b"a\r\n.loil/\r\nb\r\n")
    assert gi.has_line(tmp_path, ".loil/")
    assert gi.ensure_line(tmp_path, ".loil/") is False
    assert gi.ensure_line(tmp_path, "c")
    assert f.read_bytes() == b"a\r\n.loil/\r\nb\r\nc\n"
    assert gi.remove_line(tmp_path, ".loil/")
    assert f.read_bytes() == b"a\r\nb\r\nc\n"


def test_duplicate(tmp_path):
    f = tmp_path / ".gitignore"
    f.write_bytes(b"x\n.loil/\n")
    assert gi.ensure_line(tmp_path, ".loil/") is False
    assert f.read_bytes() == b"x\n.loil/\n"


def test_bom(tmp_path):
    f = tmp_path / ".gitignore"
    f.write_bytes(b"\xef\xbb\xbf.loil/\n")
    assert gi.has_line(tmp_path, ".loil/")
    assert gi.ensure_line(tmp_path, ".loil/") is False
    assert f.read_bytes() == b"\xef\xbb\xbf.loil/\n"


def test_negation_is_not_a_match(tmp_path):
    (tmp_path / ".gitignore").write_bytes(b"!.loil/\n")
    assert not gi.has_line(tmp_path, ".loil/")
    assert gi.ensure_line(tmp_path, ".loil/")


def test_remove(tmp_path):
    f = tmp_path / ".gitignore"
    f.write_bytes(b"x\n.loil/\ny\n")
    assert gi.remove_line(tmp_path, ".loil/")
    assert f.read_bytes() == b"x\ny\n"
    assert gi.remove_line(tmp_path, ".loil/") is False
    assert gi.remove_line(tmp_path / "nowhere", "z") is False


def test_remove_leaving_empty(tmp_path):
    f = tmp_path / ".gitignore"
    f.write_bytes(b".loil/\n")
    assert gi.remove_line(tmp_path, ".loil/")
    assert f.read_bytes() == b""


def test_non_utf8_preserved(tmp_path):
    f = tmp_path / ".gitignore"
    f.write_bytes(b"caf\xe9\n.loil/\n")
    assert gi.ensure_line(tmp_path, "z")
    assert f.read_bytes() == b"caf\xe9\n.loil/\nz\n"
    assert gi.remove_line(tmp_path, ".loil/")
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
    (tmp_path / ".gitignore").write_text(".loil/record/ \nkeep\n")
    assert gi.has_line(tmp_path, ".loil/record/")
    assert gi.remove_line(tmp_path, ".loil/record/") and (tmp_path / ".gitignore").read_text() == "keep\n"


def test_unreadable_gitignore_is_a_loi_error(tmp_path):
    from loil.errors import LoilError

    (tmp_path / ".gitignore").mkdir()
    with pytest.raises(LoilError, match="cannot read"):
        gi.has_line(tmp_path, "x")
