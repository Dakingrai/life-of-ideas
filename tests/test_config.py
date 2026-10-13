import re
import tomllib

import pytest

from loi.record import config
from loi.util import ulid
from loi.errors import LoiError, SchemaError
from loi.record.project import Project

ID = "01ARZ3NDEKTSV4RRFFQ69G5FAV"


@pytest.fixture
def p(tmp_path):
    return Project(tmp_path)


def write(p, text):
    p.loi_dir.mkdir(exist_ok=True)
    p.config_path.write_text(text)


def write_local(p, text):
    p.loi_dir.mkdir(exist_ok=True)
    p.local_path.write_text(text)


def test_missing_config_raises_and_local_defaults(p):
    assert not config.config_exists(p)
    with pytest.raises(LoiError, match="loi init"):
        config.load_config(p)
    loc = config.load_local(p)
    assert (loc.schema, loc.home) == (1, None)
    assert not hasattr(loc, "inject_context") and not hasattr(loc, "capture")
    assert not hasattr(loc, "id") and not hasattr(loc, "agents") and not hasattr(loc, "enabled_agents")
    assert not hasattr(config, "DEFAULT_AGENTS")


def test_new_config():
    a, b = config.new_config("proj"), config.new_config("proj")
    assert ulid.is_valid(a.id) and a.id != b.id
    assert (a.schema, a.name, a.loi_version) == (1, "proj", "0.2.0")
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", a.created)


def test_save_text_and_round_trip(p):
    cfg = config.new_config("proj")
    config.save_config(p, cfg)
    text = p.config_path.read_text()
    assert text == (
        f'schema = 1\nid = "{cfg.id}"\nname = "proj"\ncreated = "{cfg.created}"\nloi_version = "0.2.0"\n'
    )
    assert tomllib.loads(text) == {
        "schema": 1, "id": cfg.id, "name": "proj", "created": cfg.created, "loi_version": "0.2.0"
    }
    assert config.load_config(p) == cfg
    assert config.config_exists(p)
    cfg2 = config.Config(id=cfg.id, name="renamed", created=cfg.created, loi_version="9")
    config.save_config(p, cfg2)
    assert config.load_config(p) == cfg2
    assert config.load_config(p).id == cfg.id


def test_save_validates(p):
    with pytest.raises(LoiError):
        config.save_config(p, config.Config(id="nope", name="x", created="c", loi_version="v"))
    with pytest.raises(LoiError, match="absolute"):
        config.save_local(p, config.Local(home="rel/x"))
    assert not p.loi_dir.exists()


def test_del_character_survives_the_toml_writer(p, tmp_path):
    home = str(tmp_path / "odd\x7fdir")
    config.save_local(p, config.Local(home=home))
    assert config.load_local(p).home == home  # json.dumps leaves DEL raw; TOML forbids it


def test_local_round_trip(p):
    config.save_local(p, config.Local())
    assert p.local_path.read_text() == 'schema = 1\n'
    assert config.load_local(p) == config.Local()


def test_local_home_round_trip(p, tmp_path):
    home = str(tmp_path / "store dir")
    loc = config.Local(home=home)
    config.save_local(p, loc)
    text = p.local_path.read_text()
    assert text == f'schema = 1\nhome = "{home}"\n'
    assert config.load_local(p) == loc
    # unknown keys (`id`, `agents`, `capture`) are ignored on load and gone after a save
    write_local(p, f'schema = 1\nid = "{ID}"\nhome = "{home}"\nagents = ["claude-code"]\ncapture = "full"\n')
    assert config.load_local(p) == loc
    write_local(p, 'schema = 1\nid = 3\nagents = "nope"\n')  # not even validated
    assert config.load_local(p) == config.Local()
    config.save_local(p, config.load_local(p))
    assert p.local_path.read_text() == 'schema = 1\n'


@pytest.mark.parametrize("line", ['home = "relative/path"', "home = true", "home = 3"])
def test_local_bad_home(p, line):
    write_local(p, f"schema = 1\n{line}\n")
    with pytest.raises(SchemaError, match="home"):
        config.load_local(p)


def test_schema_99(p):
    write(p, f'schema = 99\nid = "{ID}"\n')
    with pytest.raises(SchemaError, match="99"):
        config.load_config(p)


def test_schema_string(p):
    write(p, f'schema = "1"\nid = "{ID}"\n')
    with pytest.raises(SchemaError):
        config.load_config(p)


def test_config_missing_field(p):
    write(p, f'schema = 1\nid = "{ID}"\ncreated = "c"\nloi_version = "v"\n')
    with pytest.raises(SchemaError, match="name"):
        config.load_config(p)
    write(p, f'schema = 1\nid = "{ID}"\nname = 3\ncreated = "c"\nloi_version = "v"\n')
    with pytest.raises(SchemaError, match="name"):
        config.load_config(p)


def test_bad_id(p):
    write(p, 'schema = 1\nid = "nope"\n')
    with pytest.raises(SchemaError, match="id"):
        config.load_config(p)


def test_not_toml(p):
    write(p, "schema = [\n")
    with pytest.raises(SchemaError, match="TOML"):
        config.load_config(p)


def test_unknown_key_ignored(p):
    write(p, f'schema = 1\nid = "{ID}"\nname = "x"\ncreated = "c"\nloi_version = "v"\nfoo = 3\n# a comment\n')
    assert config.load_config(p).id == ID




def test_other_schema_is_refused_naming_it(p):
    write(p, f'schema = 2\nid = "{ID}"\nname = "x"\ncreated = "c"\nloi_version = "v"\n')
    with pytest.raises(SchemaError, match="declares schema 2; this loi understands 1"):
        config.load_config(p)
    write_local(p, 'schema = 2\n')
    with pytest.raises(SchemaError, match="schema 2"):
        config.load_local(p)
