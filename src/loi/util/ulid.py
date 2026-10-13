"""ULIDs: 48-bit ms timestamp + 80 random bits, Crockford base32, monotonic per process.

The alphabet is ASCII-sorted, so `sorted(ids)` is time order. Within one process, ids
issued in the same millisecond increment the previous random part (the spec's monotonic
mode), so `07`'s batch appends stay ordered. Two processes in the same millisecond can
still interleave; file order stays authoritative. Stdlib only.
"""

import os
import threading

from loi.util import clock

ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_DECODE = {c: i for i, c in enumerate(ALPHABET)}
_MAX_MS = (1 << 48) - 1
_lock = threading.Lock()
_last_ms = -1
_last_rand = 0


def new(ms: int | None = None) -> str:
    global _last_ms, _last_rand
    ts = clock.now_ms() if ms is None else ms
    if not 0 <= ts <= _MAX_MS:
        raise ValueError(f"timestamp {ts} ms does not fit in 48 bits")
    with _lock:
        if ms is None and ts < _last_ms:  # wall clock went backwards: stay monotonic
            ts = _last_ms
        if ts == _last_ms:  # same ms: increment the previous random part
            rand = _last_rand + 1
            if rand >= 1 << 80:  # astronomically unlikely; roll into the next ms
                ts += 1
                rand = _rand80()
        else:  # a new ms (an explicit `ms` is honoured verbatim, even if older)
            rand = _rand80()
        _last_ms, _last_rand = ts, rand  # state = the last id issued, whatever its ms
    return _encode((ts << 80) | rand)


def _rand80() -> int:
    return int.from_bytes(os.urandom(10), "big")  # `secrets` would cost ~4 ms at import


def _encode(n: int) -> str:
    out = []
    for _ in range(26):
        out.append(ALPHABET[n & 31])
        n >>= 5
    return "".join(reversed(out))


def timestamp_ms(ulid: str) -> int:
    if not is_valid(ulid):
        raise ValueError(f"not a ULID: {ulid!r}")
    n = 0
    for c in ulid[:10]:
        n = (n << 5) | _DECODE[c]
    return n


def is_valid(s: str) -> bool:
    return isinstance(s, str) and len(s) == 26 and all(c in _DECODE for c in s) and s[0] <= "7"
