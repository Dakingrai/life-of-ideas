"""Project root discovery, the paths under `<root>/.loil/` (`config.toml`, `local.toml`)
and under `<loil_home>/projects/<id>/` (the person's store: session files, raw captures,
snapshots, log). Stdlib only."""

import os
from dataclasses import dataclass
from pathlib import Path

from loil.errors import LoilError

_NO_ID = "project id unknown; run `loil init`"
NOT_A_PROJECT = "not a loil project here (no .loil/config.toml in this directory or above); run `loil init`"


def loil_home(pinned: Path | str | None = None) -> Path:
    """The store root: `$LOIL_HOME`, else the checkout's pinned `local.toml: home`, else
    `~/.loil`. Evaluated per call (tests set the env). Always absolute: a relative
    `LOIL_HOME` (or `HOME=.`) must not put the store inside whatever cwd a hook runs in."""
    env = os.environ.get("LOIL_HOME")
    if env:
        return Path(os.path.abspath(os.path.expanduser(env)))
    if pinned and str(pinned) not in ("", "."):
        return Path(os.path.abspath(os.path.expanduser(str(pinned))))
    return Path(os.path.abspath(os.path.join(os.path.expanduser("~"), ".loil")))


def ensure_loil_home(pinned: Path | str | None = None) -> Path:
    """Create `<loil_home>/projects/` private to the user (0700): it holds transcripts."""
    home = loil_home(pinned)
    try:
        for d in (home, home / "projects"):
            d.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:  # LOIL_HOME is a file, HOME="" -> /.loil, quota…
        raise LoilError(
            f"cannot create {home}: {exc} (set LOIL_HOME to a writable directory, or edit "
            "`home` in .loil/local.toml, then re-run `loil init`)"
        ) from None
    return home


def find_root(start: Path | str | None = None) -> Path | None:
    """Walk up from `start` (default cwd) to the first dir holding `.loil/config.toml`.

    The marker is the committed `config.toml` as a regular file, so a clone has it; a bare
    `.loil/` directory (half-created, or one whose files are ignored) is not a root, the
    way an empty `.git/` was not. A `.loil/` below another is its own project: nearest
    wins. `abspath` (lexical: `..` folded, symlinks kept) so `a/../b` starts at `b`, not at
    `a`; never `resolve()`: keep the path the user sees. Git is not consulted.
    """
    d = Path(os.path.abspath(start or os.getcwd()))
    for p in (d, *d.parents):
        if (p / ".loil" / "config.toml").is_file():
            return p
    return None


MAX_SESSION_ID_BYTES = 200  # NAME_MAX 255 minus `.json` and mkstemp's `.<name>.XXXXXXXX`


def check_session_id(session_id: str) -> str:
    """A session id comes from an untrusted hook payload and becomes a file/dir name."""
    if not isinstance(session_id, str) or not session_id:
        raise LoilError("session id must be a non-empty string")
    if (
        "/" in session_id
        or "\\" in session_id
        or ".." in session_id
        or "\x00" in session_id
        or session_id == "."
        or len(session_id.encode("utf-8", "surrogateescape")) > MAX_SESSION_ID_BYTES
    ):
        raise LoilError(f"invalid session id {session_id!r}")
    return session_id


@dataclass(frozen=True)
class Project:
    root: Path
    id: str | None = None  # central paths need it; None until config/local.toml is read
    home: Path | None = None  # the store root pinned in local.toml; None = default

    def __post_init__(self) -> None:
        # Absolute: a later chdir must not move `.loil/`; `find_root` already absolutises,
        # this covers direct construction. An empty `home` means "not pinned".
        object.__setattr__(self, "root", Path(self.root).absolute())
        if self.home is not None:
            home = str(self.home)
            object.__setattr__(self, "home", Path(home).absolute() if home not in ("", ".") else None)

    # --- repo side: <root>/.loil/ ---------------------------------------------------

    @property
    def loil_dir(self) -> Path:
        return self.root / ".loil"

    @property
    def config_path(self) -> Path:
        return self.loil_dir / "config.toml"

    @property
    def local_path(self) -> Path:
        return self.loil_dir / "local.toml"

    # --- central side: <loil_home>/projects/<id>/ -----------------------------------

    @property
    def loil_home(self) -> Path:
        """The store root this checkout uses (`$LOIL_HOME` → pin → `~/.loil`)."""
        return loil_home(self.home)

    @property
    def home_dir(self) -> Path:
        if self.id is None:
            raise LoilError(_NO_ID)
        return self.loil_home / "projects" / self.id

    @property
    def log_path(self) -> Path:
        return self.home_dir / "loil.log"

    def artifacts_dir(self, agent: str, session_id: str) -> Path:
        """`<home_dir>/sessions/<agent>/<sid>/`: the session file and its captures."""
        from loil.adapters import check_agent  # tiny, stdlib; lazy to keep this module's imports flat

        return self.home_dir / "sessions" / check_agent(agent) / check_session_id(session_id)

    # --- constructors ---------------------------------------------------------------

    @classmethod
    def open(cls, root: Path | str | None = None) -> "Project":
        """Root (walk-up from `root`, default cwd) + `id` from `.loil/config.toml` and `home`
        from `.loil/local.toml` (absent for a clone that never ran `init`). LoilError when
        no `.loil/config.toml` is found. What every command and the hooks use."""
        found = find_root(root)
        if found is None:
            raise LoilError(NOT_A_PROJECT)
        from loil.record.config import load_config, load_local  # config imports Project; keep the cycle lazy

        p = cls(found)
        local = load_local(p)  # the person's file first: it is the one they can fix
        return cls(p.root, id=load_config(p).id, home=local.home)
