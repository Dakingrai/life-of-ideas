"""The `loil hook <agent> <event>` entry: the hook handlers of every adapter.

Stdlib only, forever: this module is on the hook path. Only `os`, `sys` and the tiny
`loil.adapters` namespace at import time (never an adapter module itself).

`run` consumes the payload, echoes `loil hook: <agent> <event>` to stderr (observable in
`claude --debug`), takes the cheap exits — an unknown adapter, an empty or `{}` payload —
on the stub's import set alone, and otherwise dispatches to
`adapters.get(agent).handle(event, payload)`. Nothing is ever written to stdout: loil
injects no context. Always exit 0.

Failures are appended to the *global* fallback log `<store root>/loil.log`, where the
store root is `$LOIL_HOME` if set, else `~/.loil` — and only if that directory already
exists. The per-checkout pin in `local.toml` is not consulted here (the adapter reads it
once it has resolved the project, and logs to that project's own log from then on). Never
to a path inside the repo: the hook's cwd is the user's checkout and nothing may appear
there uninvited.
"""

import os
import sys

from loil.adapters import check_agent, is_known
from loil.errors import LoilError, SchemaError

LOG = "loil.log"


def run(argv: list[str]) -> int:
    """Hook entry: `argv == [agent, event, ...]`. Must never fail: always returns 0."""
    agent = argv[0] if argv else ""
    event = argv[1] if len(argv) > 1 else ""
    try:
        payload = _read_payload()
        sys.stderr.write(f"loil hook: {agent} {event}\n")
        check_agent(agent)  # after the echo: a bad agent word is logged, never fatal
        if not is_known(agent):
            raise LoilError(f"unknown coding-agent adapter {agent!r}")
        body = payload.strip()
        if not body or body == b"{}":  # nothing to record: stay on the stub's import set
            return 0
        from loil.adapters import get  # lazy: the adapter module only on a real payload

        get(agent).handle(event, payload)
    except Exception as exc:  # noqa: BLE001 — a hook never fails
        _log_exception(agent, event, exc)
        if isinstance(exc, SchemaError):  # say it where `--debug` shows it
            print(f"loil hook: {exc}", file=sys.stderr)
    return 0


def _read_payload() -> bytes:
    # Closed stdin -> sys.stdin is None; a TTY would block on read().
    # Bytes, not text: a non-UTF-8 payload must not become a swallowed
    # UnicodeDecodeError that silently drops the echo (and the session).
    if sys.stdin is None or sys.stdin.isatty():
        return b""
    return sys.stdin.buffer.read()


def _log_exception(agent: str, event: str, exc: BaseException) -> None:
    # Best-effort, before a project (and its own log) is known: append to the global
    # `<loil_home>/loil.log` if that directory already exists. Never inside the repo.
    try:
        home = _loil_home()
        if home is None or not os.path.isdir(home):
            return
        import time

        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with open(os.path.join(home, LOG), "a", encoding="utf-8") as f:
            f.write(f"{ts} hook {agent} {event}: {exc!r}\n")
    except Exception:  # noqa: BLE001 — logging must never raise out of a hook
        pass


def _loil_home() -> str | None:
    # The same rule as `project.loil_home()` without importing pathlib/dataclasses:
    # `$LOIL_HOME` (expanded, made absolute) else `~/.loil`. The per-checkout pin in
    # `local.toml` is not consulted here: this runs before the project is resolved.
    env = os.environ.get("LOIL_HOME")
    if env:
        return os.path.abspath(os.path.expanduser(env))
    home = os.path.expanduser("~")
    if home.startswith("~"):  # no HOME and no passwd entry: nowhere to log
        return None
    return os.path.join(os.path.abspath(home), ".loil")
