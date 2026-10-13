"""Coding-agent adapters: one per tool loil integrates (Claude Code today). An adapter
name is `^[a-z][a-z0-9-]*$` and is the key everywhere a session says which tool produced
it: `sessions/<agent>/`, `Session.agent`, and the hook contract `loil hook <agent> <event>`.

Stdlib only, and this module in particular stays tiny: `hooks.run` imports it on every
session start to validate the agent word. The adapter modules themselves are imported
lazily by `get`.
"""

from loil.errors import LoilError

KNOWN = ("claude-code",)

_FIRST = "abcdefghijklmnopqrstuvwxyz"
_REST = frozenset(_FIRST + "0123456789-")


def check_agent(name: str) -> str:
    """The adapter-name shape (`^[a-z][a-z0-9-]*$`), without importing `re`."""
    if not isinstance(name, str) or not name or name[0] not in _FIRST or not all(c in _REST for c in name):
        raise LoilError(f"invalid adapter name {name!r}; expected ^[a-z][a-z0-9-]*$")
    return name


def is_known(name: str) -> bool:
    return name in KNOWN


def get(name: str):
    """The adapter for `name` (LoilError if the shape is wrong or no such adapter)."""
    check_agent(name)
    if name not in KNOWN:
        raise LoilError(f"unknown coding-agent adapter {name!r}; known: {', '.join(KNOWN)}")
    from importlib import import_module  # importlib is already loaded by the interpreter

    return import_module(f"loil.adapters.{name.replace('-', '_')}").ADAPTER
