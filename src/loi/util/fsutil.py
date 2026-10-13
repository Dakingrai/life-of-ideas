import contextlib
import os
import tempfile
from pathlib import Path

from loi.errors import LoiError


def atomic_write_bytes(path: Path, data: bytes, mode: int | None = None) -> None:
    """Write `data` to `path` via a `mkstemp` sibling + `os.replace`; OSError -> LoiError.

    A unique temp name per writer: two processes writing the same file at once (four
    `loi init`s racing on one registry entry, `write_session` racing `loi capture`) never
    open the same temp file and never see each other's half-written bytes. The temp file
    is removed on any failure. `mode` defaults to what `open()` would have given (0666
    minus the umask), not mkstemp's 0600: config, `.gitignore` and settings files stay
    readable.
    """
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.chmod(tmp, _default_mode() if mode is None else mode)
            os.replace(tmp, path)
        except BaseException:  # incl. KeyboardInterrupt: never leave a junk temp file behind
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
    except OSError as exc:
        raise LoiError(f"cannot write {path}: {exc}") from None


def _default_mode() -> int:
    umask = os.umask(0)
    os.umask(umask)
    return 0o666 & ~umask
