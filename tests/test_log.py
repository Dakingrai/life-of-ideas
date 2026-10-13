import logging
import os
import re

import pytest

from loi import log
from loi.record.project import Project

ID = "01ARZ3NDEKTSV4RRFFQ69G5FAV"


def test_writes_utc_line(tmp_path, monkeypatch, store_home):
    monkeypatch.setenv("TZ", "America/New_York")
    p = Project(tmp_path, id=ID)
    p.home_dir.mkdir(parents=True)
    lg = log.get_logger(p)
    lg.info("hello")
    assert p.log_path == store_home / "projects" / ID / "loi.log"
    text = p.log_path.read_text()
    assert re.match(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3} INFO test_log: hello\n$", text)
    import calendar
    import time

    logged = calendar.timegm(time.strptime(text[:19], "%Y-%m-%d %H:%M:%S"))
    assert abs(logged - time.time()) < 5  # UTC, not America/New_York
    lg2 = log.get_logger(p)
    assert lg2 is lg and len(lg.handlers) == 1
    assert lg.propagate is False
    assert not (tmp_path / ".loi").exists()  # nothing in the repo


def test_no_home_dir_no_file(tmp_path, store_home):
    p = Project(tmp_path, id=ID)
    lg = log.get_logger(p)
    lg.warning("x")
    assert not p.log_path.exists() and not store_home.exists()
    assert any(isinstance(h, logging.NullHandler) for h in lg.handlers)


def test_no_id_falls_back_to_global_log(tmp_path, store_home):
    p = Project(tmp_path)
    lg = log.get_logger(p)
    lg.warning("dropped")  # must not raise: read paths call this on id-less projects
    assert any(isinstance(h, logging.NullHandler) for h in lg.handlers)
    assert not store_home.exists() and not p.loi_dir.exists()
    store_home.mkdir()
    log.get_logger(p).warning("kept")
    assert (store_home / "loi.log").read_text().endswith("WARNING test_log: kept\n")
    assert sorted(x.name for x in store_home.iterdir()) == ["loi.log"]


def test_global_log_honours_pin(tmp_path, monkeypatch):
    monkeypatch.delenv("LOI_HOME")
    monkeypatch.setenv("HOME", str(tmp_path / "h"))
    pin = tmp_path / "pin"
    pin.mkdir()
    log.get_logger(Project(tmp_path, home=pin)).warning("x")
    assert (pin / "loi.log").exists() and not (tmp_path / "h").exists()


def test_home_dir_created_after_first_get_logger(tmp_path):
    p = Project(tmp_path / "late", id=ID)
    lg = log.get_logger(p)
    lg.warning("dropped")
    p.home_dir.mkdir(parents=True)
    log.get_logger(p).warning("kept")
    assert p.log_path.read_text().endswith("kept\n")
    assert len(log.get_logger(p).handlers) == 1


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores modes")
def test_unwritable_log_does_not_raise(tmp_path):
    p = Project(tmp_path, id=ID)
    p.home_dir.mkdir(parents=True)
    p.log_path.write_text("")
    p.log_path.chmod(0o400)
    log.get_logger(p).warning("x")  # must not raise into a hook


def test_keyed_by_log_path_not_root_or_id(tmp_path, store_home, monkeypatch):
    old = Project(tmp_path, id="01ARZ3NDEKTSV4RRFFQ69G5FAV")
    new = Project(tmp_path, id="01BX5ZZKBKACTAV9WEVGEMMVRZ")  # same root, re-inited under a new id
    old.home_dir.mkdir(parents=True)
    new.home_dir.mkdir(parents=True)
    log.get_logger(old).warning("old")
    log.get_logger(new).warning("new")
    assert old.log_path.read_text().endswith("old\n")
    assert new.log_path.read_text().endswith("new\n")
    first_log = old.log_path
    other = tmp_path / "other-home"  # same id, another store root
    monkeypatch.setenv("LOI_HOME", str(other))
    (other / "projects" / old.id).mkdir(parents=True)
    log.get_logger(old).warning("elsewhere")
    assert (other / "projects" / old.id / "loi.log").read_text().endswith("elsewhere\n")
    assert first_log.read_text().endswith("old\n")  # the earlier handler was not reused
