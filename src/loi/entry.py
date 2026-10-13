"""The `loi` entry point (console script and `python -m loi`).

Stdlib only. `loi hook <agent> <event>` runs on every coding-agent session start and
end, so it is dispatched here before argparse or anything else is imported. Everything
else — including `loi hook` with fewer than two words, or with options — goes through
`loi.cli`, which owns usage errors (exit 2) and help.
"""

import sys

HOOK = "hook"


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else list(argv)
    if (
        len(args) >= 3
        and args[0] == HOOK
        and not args[1].startswith("-")
        and not args[2].startswith("-")
    ):
        from loi.hooks import run  # stdlib only; never imports argparse

        return run(args[1:])  # [agent, event, ...]
    from loi.cli import main as cli_main

    return cli_main(args)
