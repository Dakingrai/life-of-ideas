"""Tests for the `~/.local/bin/loi` shim. HOME is always a temp dir here."""

import os
import re
import shlex
import stat
import subprocess
import sys

import pytest

from loi import shim
from loi.errors import LoiError

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only shim")


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("UV_CACHE_DIR", raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    return tmp_path


def shim_file(home):
    return home / ".local" / "bin" / "loi"


def test_creates_shim(home):
    out = shim.ensure_hook_command()
    path = shim_file(home)
    assert out == str(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o755
    assert path.read_text() == shim._body(sys.executable)
    assert path.read_text() == (
        f'#!/bin/sh\n# managed by loi init\nexec {shlex.quote(sys.executable)} -m loi "$@"\n'
    )
    # The whole point: it works with an empty environment, like a Claude Code hook.
    # (A known agent: an unknown one is *logged*, and with no HOME the stub resolves the
    # store through the passwd entry — the developer's real ~/.loi.)
    p = subprocess.run(
        [out, "hook", "claude-code", "session-start"], env={}, capture_output=True, stdin=subprocess.DEVNULL
    )
    assert p.returncode == 0
    assert p.stderr == b"loi hook: claude-code session-start\n"
    assert p.stdout == b""
    p = subprocess.run([out, "hook", "x"], env={}, capture_output=True, stdin=subprocess.DEVNULL)
    assert p.returncode == 2  # one word: a usage error, never echoed
    assert not re.search(rb"^loi hook: (?!error:)", p.stderr, re.M)  # never echoed


def test_identical_managed_shim_is_not_rewritten(home):
    out = shim.ensure_hook_command()
    before = os.stat(out)
    assert shim.ensure_hook_command() == out
    after = os.stat(out)
    assert (before.st_mtime_ns, before.st_ino) == (after.st_mtime_ns, after.st_ino)


def test_rewrites_stale_managed(home):
    path = shim_file(home)
    path.parent.mkdir(parents=True)
    path.write_text('#!/bin/sh\n# managed by loi init\nexec "/old/python" -m loi "$@"\n')
    assert shim.ensure_hook_command() == str(path)
    assert path.read_text() == shim._body(sys.executable)
    assert stat.S_IMODE(path.stat().st_mode) == 0o755


def test_foreign_file_untouched(home):
    path = shim_file(home)
    path.parent.mkdir(parents=True)
    body = "#!/bin/sh\necho hi\n"
    path.write_text(body)
    assert shim.ensure_hook_command() == str(path)
    assert path.read_text() == body


def test_binary_foreign_file_untouched(home):
    path = shim_file(home)
    path.parent.mkdir(parents=True)
    body = b"\x7fELF\x02\x01\x01\x00" + b"\xff\xfe\x00" * 8
    path.write_bytes(body)
    assert shim.ensure_hook_command() == str(path)
    assert path.read_bytes() == body


def test_foreign_symlink_untouched(home):
    path = shim_file(home)
    path.parent.mkdir(parents=True)
    target = home / "other"
    target.write_text("#!/bin/sh\n# managed by loi init\n")  # even a managed-looking target
    path.symlink_to(target)
    assert shim.ensure_hook_command() == str(path)
    assert path.is_symlink() and os.readlink(path) == str(target)


def test_dangling_symlink_untouched(home):
    path = shim_file(home)
    path.parent.mkdir(parents=True)
    path.symlink_to(home / "missing")
    assert shim.ensure_hook_command() == str(path)
    assert path.is_symlink()


def test_refuses_xdg_cache(home, monkeypatch):
    exe = home / "cache" / "uv" / "builds-v0" / "x" / "bin" / "python"
    monkeypatch.setattr(sys, "executable", str(exe))
    with pytest.raises(LoiError):
        shim.ensure_hook_command()
    assert not shim_file(home).exists()


def test_refuses_uv_cache_dir(home, monkeypatch):
    cache = home / "uvc"
    monkeypatch.setenv("UV_CACHE_DIR", str(cache))
    monkeypatch.setattr(sys, "executable", str(cache / "somewhere" / "bin" / "python"))
    with pytest.raises(LoiError):
        shim.ensure_hook_command()


def test_refuses_default_home_cache(home, monkeypatch):
    monkeypatch.delenv("XDG_CACHE_HOME")
    monkeypatch.setattr(sys, "executable", str(home / ".cache" / "uv" / "a" / "bin" / "python"))
    with pytest.raises(LoiError):
        shim.ensure_hook_command()


def test_refuses_macos_cache(home, monkeypatch):
    exe = home / "Library" / "Caches" / "uv" / "a" / "bin" / "python"
    monkeypatch.setattr(sys, "executable", str(exe))
    with pytest.raises(LoiError):
        shim.ensure_hook_command()


@pytest.mark.parametrize("store", ["builds-v0", "archive-v0"])
def test_refuses_no_cache_ephemeral_env(home, monkeypatch, store):
    # `uv --no-cache` / UV_NO_CACHE=1 build the ephemeral env under $TMPDIR/.tmpXXXX,
    # outside every cache dir; the uv store layout is the only remaining signal.
    exe = home / "elsewhere" / ".tmpAb12" / store / "x" / "bin" / "python"
    monkeypatch.setattr(sys, "executable", str(exe))
    with pytest.raises(LoiError):
        shim.ensure_hook_command()
    assert not shim_file(home).exists()


def test_sibling_of_cache_allowed(home, monkeypatch):
    monkeypatch.setattr(sys, "executable", str(home / "cache" / "uv-other" / "bin" / "python"))
    shim.ensure_hook_command()
    assert shim_file(home).exists()


def test_awkward_interpreter_path_is_quoted(home, monkeypatch):
    # A fake "interpreter" at a path with a space and a quote: it just reports its argv.
    # (A symlink to the real venv python would lose the venv — that is the very reason
    # the shim writes sys.executable verbatim, so it is not usable here.)
    weird = home / "odd dir" / "py'thon"
    weird.parent.mkdir()
    weird.write_text('#!/bin/sh\nprintf "ARG:%s\\n" "$@" >&2\n')
    weird.chmod(0o755)
    monkeypatch.setattr(sys, "executable", str(weird))
    out = shim.ensure_hook_command()
    assert shlex.quote(str(weird)) in shim_file(home).read_text()
    p = subprocess.run([out, "hook", "q", "r"], env={}, capture_output=True, stdin=subprocess.DEVNULL)
    assert p.returncode == 0
    assert p.stderr == b"ARG:-m\nARG:loi\nARG:hook\nARG:q\nARG:r\n"


SHIM_WRITER = """
import sys
from loi import shim
STALE = '#!/bin/sh\\n# managed by loi init\\nexec "/old/python" -m loi "$@"\\n'
path = shim.shim_path()
for _ in range(50):
    try:
        path.write_text(STALE)  # make it stale again so every round really rewrites
    except OSError:
        pass  # another process is mid-replace; fine
    shim.ensure_hook_command()
"""


def test_concurrent_managed_shim_writes_never_fail(home):
    path = shim_file(home)
    path.parent.mkdir(parents=True)
    path.write_text('#!/bin/sh\n# managed by loi init\nexec "/old/python" -m loi "$@"\n')
    procs = [
        subprocess.Popen([sys.executable, "-c", SHIM_WRITER], stderr=subprocess.PIPE, env=dict(os.environ))
        for _ in range(4)
    ]
    for pr in procs:
        _, err = pr.communicate()
        assert pr.returncode == 0, err  # a fixed `loi.tmp` made 9 of 40 of these fail
    # the last writer wins with a complete body, either the current one or a whole stale one
    assert path.read_text() in (shim._body(sys.executable), '#!/bin/sh\n# managed by loi init\nexec "/old/python" -m loi "$@"\n')
    assert shim.ensure_hook_command() == str(path) and path.read_text() == shim._body(sys.executable)
    assert stat.S_IMODE(path.stat().st_mode) == 0o755
    assert sorted(x.name for x in path.parent.iterdir()) == ["loi"]  # no temp files left


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores modes")
def test_unwritable_parent(home):
    d = home / ".local"
    d.mkdir()
    d.chmod(0o500)
    try:
        with pytest.raises(LoiError):
            shim.ensure_hook_command()
    finally:
        d.chmod(0o700)
