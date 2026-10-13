"""UTC millisecond clock and the one timestamp shape loi writes.

`YYYY-MM-DDTHH:MM:SS.mmmZ` — always UTC, always milliseconds, always a literal `Z`.
Readers accept `+00:00` too. Stdlib only.
"""

import time
from datetime import datetime, timezone

UTC = timezone.utc  # not datetime.UTC: explicit, and fine on the 3.11 floor


def now_ms() -> int:
    return int(time.time() * 1000)


def now_iso(ms: int | None = None) -> str:
    """The timestamp for `ms` (default: now), formatted from integers — truncating."""
    if ms is None:
        ms = now_ms()
    dt = datetime.fromtimestamp(ms // 1000, UTC)
    return f"{dt:%Y-%m-%dT%H:%M:%S}.{ms % 1000:03d}Z"


def parse_iso(s: str) -> datetime:
    """Accepts a trailing 'Z' or an explicit offset; returns an aware datetime in UTC."""
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)
