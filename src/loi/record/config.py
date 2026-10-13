"""`.loi/config.toml` (per project, committed: the project's id and the root marker) and
`.loi/local.toml` (per user, ignored: the store root this checkout uses).

Read with `tomllib`, written by hand (a handful of keys). Unknown keys are ignored on
load and not preserved on save. Stdlib only.
"""

import json
import os
import tomllib
from dataclasses import dataclass, fields

from loi.util import clock, ulid
from loi.errors import LoiError, SchemaError
from loi.util.fsutil import atomic_write_bytes
from loi.record.project import Project

SCHEMA = 1


@dataclass(kw_only=True)
class Config:
    schema: int = SCHEMA
    id: str
    name: str
    created: str  # ISO-8601 UTC, stamped once by `init`
    loi_version: str  # the loi that created the project


@dataclass(kw_only=True)
class Local:
    schema: int = SCHEMA
    home: str | None = None  # the absolute store root `init` resolved; None = key absent


def loi_version() -> str:
    """The installed package's version (lazy: ~70 ms; only `init` asks, never a hook)."""
    try:
        from loi import __version__
    except Exception:  # noqa: BLE001 — metadata missing in odd installs
        return "unknown"
    return __version__


def new_config(name: str) -> Config:
    """A fresh project config: new ULID `id`, `created` now, `loi_version` from the
    installed package. Save it if you want it kept."""
    return Config(id=ulid.new(), name=name, created=clock.now_iso(), loi_version=loi_version())


def config_exists(project: Project) -> bool:
    return project.config_path.is_file()


def load_config(project: Project) -> Config:
    """Raises LoiError when the file is missing: defaults would mint a different `id`
    per call. `init` uses `config_exists` + `new_config`."""
    path = project.config_path
    if not path.is_file():
        raise LoiError(f"no {path} here — run `loi init`")
    d = _read(path)
    _check_schema(path, d)
    cfg = Config(
        id=_str(path, d, "id"),
        name=_str(path, d, "name"),
        created=_str(path, d, "created"),
        loi_version=_str(path, d, "loi_version"),
    )
    if not ulid.is_valid(cfg.id):
        raise SchemaError(f"{path}: id {cfg.id!r} is not a ULID")
    return cfg


def save_config(project: Project, cfg: Config) -> None:
    if not ulid.is_valid(cfg.id):
        raise LoiError(f"config id {cfg.id!r} is not a ULID")
    _write(project.config_path, cfg)


def load_local(project: Project) -> Local:
    """A missing file means defaults: a collaborator who cloned but never ran `init`."""
    path = project.local_path
    if not path.is_file():
        return Local()
    d = _read(path)
    _check_schema(path, d)
    local = Local()
    if "home" in d:
        local.home = _str(path, d, "home")
        if not os.path.isabs(local.home):
            raise SchemaError(f"{path}: home must be an absolute path, got {local.home!r}")
    return local


def save_local(project: Project, local: Local) -> None:
    if local.home is not None and not os.path.isabs(local.home):
        raise LoiError(f"local home must be an absolute path, got {local.home!r}")
    _write(project.local_path, local)


# --- helpers ---------------------------------------------------------------


def _read(path) -> dict:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise SchemaError(f"{path}: not valid TOML: {exc}") from None
    except OSError as exc:
        raise SchemaError(f"cannot read {path}: {exc}") from None


def _check_schema(path, d: dict) -> None:
    schema = d.get("schema")
    if type(schema) is int and schema == SCHEMA:
        return
    raise SchemaError(f"{path} declares schema {schema!r}; this loi understands {SCHEMA}")


def _str(path, d, key) -> str:
    v = d.get(key)
    if not isinstance(v, str):
        raise SchemaError(f"{path}: {key} must be a string, got {v!r}")
    return v


def _write(path, obj) -> None:
    lines = []
    for f in fields(obj):
        v = getattr(obj, f.name)
        if v is None:
            continue  # TOML has no null: an unset optional is an absent key
        if isinstance(v, bool):
            s = "true" if v else "false"
        elif isinstance(v, int):
            s = str(v)
        else:
            # str -> a TOML basic string; list[str] -> a TOML array of them (same syntax).
            # JSON leaves DEL (0x7f) raw; TOML forbids it in a basic string.
            s = json.dumps(v, ensure_ascii=False).replace("\x7f", "\\u007f")
        lines.append(f"{f.name} = {s}\n")
    atomic_write_bytes(path, "".join(lines).encode("utf-8"))
