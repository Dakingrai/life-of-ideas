"""The argparse CLI: `init`, `capture`, `ls`, `verify`, and `hook`'s usage/help.

Never on the hook path: `entry.main` dispatches `hook <agent> <event>` before importing
this module. Commands live in `loi.commands.*` and are imported lazily per command.
"""

import argparse
import sys

from loi.errors import LoiError

PROG = "loi"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=PROG, description="Back up Claude Code sessions and the metadata around them.")
    p.add_argument("--version", action="store_true", help="print the version and exit")
    sub = p.add_subparsers(dest="command", metavar="<command>")

    init = sub.add_parser("init", help="make this folder a loi-tracked project and install the hooks")
    init.add_argument("--no-backfill", action="store_true", help="do not import the sessions Claude Code already holds")
    init.add_argument("--home", metavar="PATH", help="pin this store root for the checkout (a set $LOI_HOME still wins; default ~/.loi)")

    cap = sub.add_parser("capture", help="copy a session's artifacts into the store now")
    cap.add_argument("--session", metavar="ID", help="the session to capture (default: the newest open one)")
    cap.add_argument("--backfill", action="store_true", help="import every session Claude Code still holds for this project")

    sub.add_parser("ls", help="list the sessions recorded for this project")
    sub.add_parser("verify", help="re-hash every stored artifact against its session file")

    hook = sub.add_parser("hook", help="hook entry used by the coding agent (loi hook <agent> <event>)")
    hook.add_argument("agent", help="coding-agent adapter name, e.g. claude-code")
    hook.add_argument("event", help="hook event word, e.g. session-start")
    return p


def main(argv: list[str]) -> int:
    parser = build_parser()
    ns = parser.parse_args(argv)  # usage errors exit 2 here
    if ns.version:
        import loi

        print(f"{PROG} {loi.__version__}")
        return 0
    if ns.command is None:
        parser.print_help()
        return 0
    try:
        return _dispatch(ns)
    except LoiError as exc:
        print(f"{PROG} {ns.command}: {exc}", file=sys.stderr)
        return 1


def _dispatch(ns) -> int:
    if ns.command == "hook":  # only reachable with options; the real path is entry.main
        from loi.hooks import run

        return run([ns.agent, ns.event])
    if ns.command == "init":
        from loi.commands.init import run_init

        return run_init(backfill=not ns.no_backfill, home=ns.home)
    if ns.command == "capture":
        from loi.commands.capture import run_capture

        return run_capture(session=ns.session, backfill=ns.backfill)
    if ns.command == "ls":
        from loi.commands.ls import run_ls

        return run_ls()
    if ns.command == "verify":
        from loi.commands.verify import run_verify

        return run_verify()
    raise LoiError(f"unknown command {ns.command!r}")
