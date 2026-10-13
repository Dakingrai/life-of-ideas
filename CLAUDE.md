# CLAUDE.md

Read this before doing anything in this repo.

## What loi is

`loi` ("life of ideas, light") backs up every Claude Code session of a repo, plus the
metadata needed to later reconstruct how the idea evolved, into a private store outside the
repo (`~/.loi`). Nothing else: no events, no context injection, no dashboard, no model calls.
The analysis happens later, from the backup. It exists because Claude Code deletes its
transcripts after ~30 days.

What one session leaves in `~/.loi/projects/<id>/sessions/claude-code/<sid>/`: the raw hook
payloads, the transcript, everything in the session's own directory (subagents, tool results),
every plan file it named, Claude's `file-history/` for the session, git HEAD/branch at start and
HEAD/branch/status/diff at end, the project's entry from `~/.claude.json`. Per project:
hashed snapshots of Claude's project memory and of the repo's agent config (`memory/`,
`repo-config/`), the project's lines of Claude's prompt history with their pastes, and
Claude's `sessions-index.json`. `loi verify` re-hashes all of it.

## How it's built

Python ≥ 3.11, uv, **no runtime dependencies** (argparse, stdlib only). Write every feature so
these stay true:

- **Everything users hold is language-neutral.** JSON/JSONL/TOML with a schema version
  (`schema` in config, `v` on session files), ISO-8601 UTC timestamps, no NaN. The repo holds
  exactly `.loi/config.toml` (the root marker and the id; the root is the nearest one, git is
  not consulted) and `.loi/local.toml` (this person's store root, ignored). Everything else is
  in the store, keyed by project id, sessions under `sessions/<agent>/<sid>/`.
- **The CLI surface is the contract.** `init`, `capture`, `ls`, `verify`, `hook <agent> <event>`
  and their exit codes (0 ok, 1 failure, 2 usage) are what users and hooks depend on. Extend
  them; don't rename or repurpose. Pin them with subprocess tests.
- **What gets written into users' repos must outlive our install.** Hook commands point at
  the stable `~/.local/bin/loi` shim, never a `.venv/` or interpreter path.
- **The hook path stays light.** `loi hook` runs on every session start and end; it must not
  import argparse or anything beyond stdlib (`tests/test_import_purity.py`,
  `test_hooks.py::test_import_set_per_path`), and session-start is measured (best-of-5, soft
  150 ms, hard 500 ms).
- **Hooks never raise, always exit 0, never write into the repo, never create a session on
  end/pre-compact, never create the store.** Git is read with `GIT_OPTIONAL_LOCKS=0` and a
  2 s timeout so `git status` cannot touch `.git/index`.
- **The store is private.** Dirs 0700, files 0600. It may hold secrets Claude's own files hold
  (MCP env in `.claude/settings*.json`, the `~/.claude.json` entry); it never leaves the machine.
- **Capture is idempotent and byte-faithful.** Each artifact is recorded with the source's
  `bytes` and `sha256`; `.jsonl`/`.txt`/`.md` are gzipped (mtime 0), `gunzip -c` returns the
  original. Names under a session dir that loi owns (`store.RESERVED`) are never overwritten
  by a Claude-side file.

## How to work here

- **Tests first.** Each change starts with its test; `uv run pytest -q` is green before it ends.
- **One change per turn, shown, then stop.** Do the single thing that was asked, touching only
  the files it needs. Follow-ups are offered, not done.
- The plan this was built from: `~/.claude/plans/dapper-wiggling-walrus.md`.
