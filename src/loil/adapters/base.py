"""The adapter interface. Stdlib only (the handlers run on the hook path).

An adapter with hooks lists them in `hook_events`; `loil init` installs
`<shim> hook <name> <event>` for each into the tool's settings file. A passive adapter
(a tool with no hooks) has `hook_events == ()` and relies on `discover` (find the sessions
the tool left on disk) and `capture` (copy their artifacts).

A plain class, not a dataclass: `dataclasses` costs ~14 ms on the hook path.
"""

from pathlib import Path


class Source:
    """One artifact to store: `kind` (transcript|subagent|tool-result|plan|file-history|
    payload|git|claude-project-entry|other), the absolute `src` on disk — or `data`, the
    bytes themselves, for content loil generates (a hook payload, a git snapshot) — and
    `dst_rel` under the session's artifacts dir (`.gz` appended by the adapter when the
    copy is gzipped; None for a plan, named by content hash). `feed`, if set, receives
    every chunk read from `src`."""

    __slots__ = ("kind", "src", "dst_rel", "feed", "data")

    def __init__(self, kind, src, dst_rel, feed=None, *, data=None):
        if (src is None) == (data is None):
            raise ValueError("Source needs exactly one of src and data")
        self.kind, self.src, self.dst_rel, self.feed, self.data = kind, src, dst_rel, feed, data

    def __repr__(self):
        return f"<Source {self.kind} {self.src if self.src is not None else '<data>'} -> {self.dst_rel}>"


class Discovered:
    """A session found on disk by `Adapter.discover`: `id`, its `transcript_path`, and
    `start`/`end` in loil's timestamp shape (None when the transcript carries none)."""

    __slots__ = ("id", "transcript_path", "start", "end")

    def __init__(self, id, transcript_path, start, end):
        self.id, self.transcript_path, self.start, self.end = id, transcript_path, start, end

    def __repr__(self):
        return f"<Discovered {self.id} {self.start}..{self.end}>"


class Adapter:
    #: `^[a-z][a-z0-9-]*$`; the directory name under `sessions/`
    name: str = ""
    #: (settings key, `loil hook <name> <event>` event word, timeout in seconds or None)
    hook_events: tuple[tuple[str, str, int | None], ...] = ()
    #: Whether `loil init` should add this adapter's settings file to `.gitignore`.
    settings_are_local: bool = True

    def settings_path(self, root: Path) -> Path | None:
        """The per-user hook settings file of this tool in a checkout, or None."""
        return None

    def handle(self, event: str, payload: bytes) -> None:
        """`loil hook <name> <event>` with a non-empty payload (raw stdin bytes). Stdlib
        only; never imports argparse. Raise while the project is still unknown (the global
        log), log to the project's own log afterwards — the hook always exits 0 either way.
        Writes nothing to stdout: loil injects no context."""
        return None

    def sources(self, session):
        """The raw artifacts of `session` on this machine, in copy order, as `Source`s:
        the transcript first. A source whose `feed` is set gets every chunk of its bytes
        while it is read (the plan-file scan rides the transcript read pass), and the
        list may be a generator that yields plan sources *after* the transcripts were
        read. Raise LoilError when the session cannot be captured here (no
        `transcript_path`, transcript gone)."""
        return []

    def discover(self, root):
        """The sessions this tool still holds on disk for the checkout at `root`
        (`Discovered`s, oldest first), for backfill."""
        return []

    def __repr__(self) -> str:
        return f"<Adapter {self.name}>"
