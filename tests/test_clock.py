import re
from datetime import datetime, timezone

from loi.util import clock

RX = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")


def test_now_iso_shape():
    assert RX.match(clock.now_iso())


def test_round_trip_and_offset():
    s = clock.now_iso()
    dt = clock.parse_iso(s)
    assert dt.tzinfo is not None and dt.utcoffset().total_seconds() == 0
    assert clock.parse_iso(s[:-1] + "+00:00") == dt
    assert clock.now_iso(int(dt.timestamp() * 1000)) == s


def test_epoch_and_millis():
    assert clock.now_iso(0) == "1970-01-01T00:00:00.000Z"
    assert clock.now_iso(999) == "1970-01-01T00:00:00.999Z"
    assert clock.now_iso(1000) == "1970-01-01T00:00:01.000Z"
    assert clock.parse_iso("1970-01-01T00:00:00.000Z") == datetime(1970, 1, 1, tzinfo=timezone.utc)


def test_parse_naive_and_offset():
    assert clock.parse_iso("2026-08-26T10:00:00") == datetime(2026, 8, 26, 10, tzinfo=timezone.utc)
    assert clock.parse_iso("2026-08-26T12:00:00+02:00") == datetime(2026, 8, 26, 10, tzinfo=timezone.utc)
