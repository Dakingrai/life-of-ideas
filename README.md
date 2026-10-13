# loi — life of ideas, light

Claude Code deletes its transcripts after about thirty days. `loi` keeps them, and
everything around them that you would need later to see how an idea evolved, in a private
store on your disk. It does nothing else.

**Everything stays on your machine. `loi` never uploads, syncs or sends anything anywhere.**
It has no network code, no account, no telemetry and no cloud. The store is a folder on
your own disk (`~/.loi`, readable only by you), and the only things that leave it are the
ones you move yourself.

## Install

```sh
uv tool install loi-light    # or: pipx install loi-light
```

The package on PyPI is `loi-light`; the command it installs is `loi`. Python 3.11 or
newer, no dependencies.

## Use

```sh
cd ~/your/repo
loi init                    # once per clone: installs the hooks, imports the sessions Claude Code still holds
loi ls                      # the sessions recorded for this repo
loi verify                  # re-hash every stored file against its record
loi capture                 # copy the newest open session now (--session ID for another one)
loi capture --backfill      # import every session Claude Code still holds (e.g. after a reinstall)
```

`loi init` prints what it touched:

```
loi init: /Users/you/your/repo
  .loi/config.toml            created (id 01M4SMD0F7ATJM7CKTP0Z29MS0)
  .loi/local.toml             created (home = ~/.loi)
  .gitignore                   created: .loi/local.toml, .claude/settings.local.json
  .claude/settings.local.json  hooks installed
  ~/.loi/projects/01M4SMD0F7ATJM7CKTP0Z29MS0/ created
  registry                     added
  hook command: ~/.local/bin/loi
Commit .loi/config.toml and .gitignore. Each collaborator runs `loi init` once per clone.
```

Three files land in the repo. `.loi/config.toml` is the project's id: commit it, so every
clone records into the same project. `.loi/local.toml` (which store this checkout uses) and
`.claude/settings.local.json` (the hooks) are per user and go into `.gitignore`.

The hooks are `SessionStart`, `SessionEnd` and `PreCompact`, each running
`~/.local/bin/loi hook claude-code <event>`. They always exit 0, never write into the repo,
and read git with optional locks off, so Claude Code never notices them. Session start
records the hook payload and git HEAD; session end and every compaction copy the session's
files into the store. `loi init` is idempotent: running it again repairs rather than
duplicates. Pass `--no-backfill` to skip the import of existing sessions.

## What it keeps

Everything lives under `~/.loi/projects/<id>/`. For every session, in
`sessions/claude-code/<session-id>/`:

| in the store | what |
|---|---|
| `session.json` | the record: id, start, end, title, every artifact with its size and sha256 |
| `payloads/<ts>-<event>.json` | the raw hook payloads (start, end, every compaction) |
| `transcript.jsonl.gz` | the conversation, byte for byte |
| `subagents/`, `tool-results/`, … | everything in the session's own Claude directory |
| `plans/<sha>.md` | every plan file the session named, every version, by content hash |
| `file-history/*.gz` | Claude's copies of every file version it edited in the session |
| `git/start.json`, `git/end.json`, `git/diff-end.patch.gz` | HEAD, branch, status and the uncommitted diff |
| `claude-project-entry.json` | the project's entry in `~/.claude.json` (cost, tokens, model) |

And per project, next to `sessions/`:

| in the store | what |
|---|---|
| `memory/<ts>/` + `memory/index.jsonl` | Claude's project memory, snapshotted whenever its content changes |
| `repo-config/<ts>/` + `repo-config/index.jsonl` | `CLAUDE.md`, `AGENTS.md`, `.claude/*.json`, agents, skills, commands, hooks, rules; same scheme |
| `history.jsonl`, `pastes/<hash>.txt` | your prompt history for this project, with the pasted text it references |
| `sessions-index.json` | Claude's own index of the project's sessions |
| `project.json`, `loi.log` | the project record and loi's own log |

Everything is plain files: JSON, JSONL, gzip. `gunzip -c` gives you the original. Each file is
recorded with its size and sha256, and `loi verify` checks them all. Files over 200 MB are
skipped and logged.

## Where the store is

`~/.loi` by default (dirs 0700, files 0600). `LOI_HOME` in the environment wins; otherwise
the path pinned in `.loi/local.toml` by `loi init --home PATH`; otherwise `~/.loi`.

The store may hold what Claude's own files hold, including MCP environment values from
`.claude/settings*.json` and anything you pasted into a session. **Nothing in it is ever
transmitted: it stays local unless you copy it somewhere yourself.** If you point
`LOI_HOME` at a synced folder (iCloud, Dropbox), that sync is yours, not loi's.

## Stopping

`uv tool uninstall loi-light` (or `pipx uninstall loi-light`), then remove the three loi
entries from `.claude/settings.local.json`, or the file itself if nothing else is in it. The
store is yours: it stays until you delete it.

## Status

Pre-alpha. Hooks, capture, backfill, `ls` and `verify` work for Claude Code on Linux and
macOS. Not yet: importing sessions from a `loi` store, a periodic capture to bound loss on a
crash, Codex.

## License

Apache-2.0
