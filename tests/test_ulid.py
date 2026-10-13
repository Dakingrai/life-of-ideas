import pytest

from loi.util import ulid


def test_shape():
    u = ulid.new()
    assert len(u) == 26
    assert set(u) <= set(ulid.ALPHABET)
    assert u[0] <= "7"
    assert ulid.is_valid(u)


def test_timestamp_round_trip():
    for ms in (0, 1, 1_000, 1_756_000_000_000, (1 << 48) - 1):
        assert ulid.timestamp_ms(ulid.new(ms)) == ms


def test_known_encodings():
    assert ulid._encode(0) == "0" * 26
    assert ulid._encode((1 << 128) - 1) == "7" + "Z" * 25


def test_burst_is_strictly_increasing_and_unique():
    ids = [ulid.new() for _ in range(2000)]
    assert ids == sorted(ids)
    assert all(a < b for a, b in zip(ids, ids[1:]))
    assert len(set(ids)) == 2000


def test_same_ms_twice_increases():
    a = ulid.new(1_700_000_000_000)
    b = ulid.new(1_700_000_000_000)
    assert b > a
    assert ulid.timestamp_ms(b) == ulid.timestamp_ms(a)


def test_is_valid_rejects():
    good = ulid.new()
    assert not ulid.is_valid(good.lower())
    assert not ulid.is_valid(good[:-1])
    for c in "ILOU":
        assert not ulid.is_valid(c + good[1:])
        assert not ulid.is_valid(good[:-1] + c)
    assert not ulid.is_valid("8" + good[1:])
    assert not ulid.is_valid("")
    assert not ulid.is_valid(None)


def test_overflow_guard():
    with pytest.raises(ValueError):
        ulid.new(1 << 48)
    with pytest.raises(ValueError):
        ulid.new(-1)


def test_explicit_older_ms_is_honoured_and_still_monotonic_within_it():
    ulid.new()  # something newer was issued
    a = ulid.new(1_000)
    b = ulid.new(1_000)
    assert ulid.timestamp_ms(a) == ulid.timestamp_ms(b) == 1_000 and b > a
    assert ulid.new() > b  # wall clock resumes normally


def test_clock_going_backwards_stays_monotonic(monkeypatch):
    from loi.util import clock

    a = ulid.new()
    monkeypatch.setattr(clock, "now_ms", lambda: 1_000)
    b = ulid.new()
    assert b > a and ulid.timestamp_ms(b) >= ulid.timestamp_ms(a)
