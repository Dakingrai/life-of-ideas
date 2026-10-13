# loil — life of ideas, light

Claude Code deletes its transcripts after about thirty days. `loil` keeps them, and
everything around them that you would need later to see how an idea evolved, in a private
store on your disk. It does nothing else.

## What it keeps

For every Claude Code session of a repo you `loil init`:

| in the store | what |
|---|---|
| `payloads/*.json` | the raw hook payloads (start, end, every compaction) |
| `transcript.jsonl.gz` | the conversation, byte for byte |
| `subagents/`, `tool-results/`, … | everything in the session's own Claude directory |
| `plans/<sha>.md` | every plan file the session named, every version |
| `file-history/*.gz` | Claude's copies of every file version it edited in the session |
| `git/start.json`, `git/end.json`, `git/diff-end.patch.gz` | HEAD, branch, status and the uncommitted diff |
| `claude-project-entry.json` | the project's entry in `~/.claude.json` (cost, tokens, model) |

And per project: snapshots of Claude's project memory and of the repo's agent configuration
(`CLAUDE.md`, `.claude/…`) whenever they change, your prompt history for the project with the
pasted text it references, and Claude's own `sessions-index.json`.

Everything is plain files: JSON, JSONL, gzip. `gunzip -c` gives you the original. Each file is
recorded with its size and sha256, and `loil verify` checks them all.

## Use

```sh
uv tool install .            # or: uv run loil …
cd ~/your/repo
loil init                    # hooks into .claude/settings.local.json; imports the sessions Claude still holds
loil ls                      # the sessions recorded for this repo
loil verify                  # re-hash every stored file
loil capture --backfill      # import again (e.g. after sessions ran on another machine)
```

The store is `~/.loil` (dirs 0700, files 0600). Set `LOIL_HOME` or `loil init --home PATH`
to keep it elsewhere. Nothing large or sensitive goes inside the repo: it gets
`.loil/config.toml` (commit it; it is the project's id) and two gitignored per-user files.

The store may hold what Claude's own files hold, including MCP environment values from
`.claude/settings*.json`. It never leaves your machine unless you move it.

## Status

Pre-alpha. Hooks, capture, backfill, `ls` and `verify` work for Claude Code. Not yet:
importing sessions from a `loi` store, a periodic capture to bound loss on a crash, Codex.

## License

Apache-2.0
