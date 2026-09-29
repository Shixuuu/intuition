"""The config surface: one schema, a validated writer, and the command boundary.

The schema is the only place a setting's default, type, and help text live, so
these tests pin the things that would otherwise drift: the template written at
init, the value each reader sees, and the fact that changing a setting from a
surface cannot drop the others.
"""

import argparse
import json
from pathlib import Path

import pytest

from intuition import config
from intuition.cli import cmd_config
from intuition.config import ConfigError


def test_schema_defaults_are_what_a_reader_sees(store):
    """An empty config file means every read returns the schema default, so the
    schema and the code that reads it cannot drift apart."""
    store.write(config.CONFIG_NAME, "")
    store.cfg = config.read(store.dir(config.CONFIG_NAME))
    for key in config.SETTINGS:
        section, _, name = key.partition(".")
        assert store.section(section, name) == config.default_of(key)


def test_unknown_key_fails_loudly(store):
    with pytest.raises(ConfigError):
        store.section("steward", "not_a_key")
    with pytest.raises(ConfigError):
        config.set_value(store, "steward.not_a_key", 1)
    with pytest.raises(ConfigError):
        config.set_value(store, "nosuchsection.key", 1)


def test_set_value_round_trips_through_the_file(store):
    config.set_value(store, "steward.idle_minutes", "12")

    assert store.section("steward", "idle_minutes") == 12
    assert "idle_minutes = 12" in store.read_text(config.CONFIG_NAME)
    from intuition.store import Store

    reopened = Store(store.root)                 # what the next process sees
    assert reopened.section("steward", "idle_minutes") == 12


def test_set_value_keeps_every_other_key(store):
    before = config.read(store.dir(config.CONFIG_NAME))
    config.set_value(store, "search.hop_decay", "0.5")
    after = config.read(store.dir(config.CONFIG_NAME))

    assert after["steward"] == before["steward"]
    assert after["search"]["max_records"] == before["search"]["max_records"]
    assert after["search"]["hop_decay"] == 0.5
    assert isinstance(after["search"]["hop_decay"], float)


def test_set_value_coerces_by_the_schema_type(store):
    assert config.set_value(store, "safety.secure_enabled", "true") is True
    assert config.set_value(store, "safety.secure_enabled", "no") is False
    assert config.set_value(store, "safety.profile_min_confidence", "0.9") == 0.9
    assert config.set_value(store, "steward.deep_time", "04:30") == "04:30"
    written = config.read(store.dir(config.CONFIG_NAME))
    assert written["safety"]["secure_enabled"] is False
    assert isinstance(written["steward"]["deep_time"], str)


def test_set_value_refuses_a_value_of_the_wrong_shape(store):
    for key, value in (("steward.idle_minutes", "soon"),
                       ("safety.secure_enabled", "maybe"),
                       ("search.hop_decay", "half")):
        with pytest.raises(ConfigError):
            config.set_value(store, key, value)


def test_nested_keys_configure_the_http_planner(store):
    config.set_value(store, "steward.llm_http.url", "http://127.0.0.1:9/v1/chat")

    assert store.cfg["steward"]["llm_http"]["url"].endswith("/chat")
    from intuition import llm

    assert llm.llm_configured(store) is True


def test_the_template_written_at_init_does_not_switch_on_model_mode(store):
    """init writes an empty [steward.llm_http]; that must stay deterministic mode."""
    from intuition import llm

    assert store.cfg["steward"]["llm_http"] == {"url": "", "model": "", "key_env": ""}
    assert llm.llm_configured(store) is False


def test_generated_template_round_trips_every_default(tmp_path):
    path = tmp_path / "intuition.toml"
    path.write_text(config.default_config_text())

    parsed = config.read(path)
    for key in config.SETTINGS:
        assert config.get_value(parsed, key) == config.default_of(key)


def test_describe_marks_what_the_file_changes(store):
    config.set_value(store, "steward.idle_minutes", 12)

    rows = {row["key"]: row for row in config.describe(store)}
    assert rows["steward.idle_minutes"]["overridden"] is True
    assert rows["steward.idle_minutes"]["value"] == 12
    assert rows["search.max_records"]["overridden"] is False
    assert rows["search.max_records"]["help"]


def test_cli_config_set_then_show(store, capsys):
    cmd_config(argparse.Namespace(store=str(store.root), action="set",
                                  key="steward.idle_minutes", value="9",
                                  json=False, value_two=None))
    assert "idle_minutes = 9" in capsys.readouterr().out

    cmd_config(argparse.Namespace(store=str(store.root), action="show", json=True))
    shown = json.loads(capsys.readouterr().out)
    assert shown["steward.idle_minutes"] == 9
    assert shown["search.max_records"] == 4


def test_cli_config_get_and_error_exit(store, capsys):
    cmd_config(argparse.Namespace(store=str(store.root), action="get",
                                  key="steward.deep_time", json=False, value=None))
    assert capsys.readouterr().out.strip() == "03:00"

    with pytest.raises(SystemExit) as exit_info:
        cmd_config(argparse.Namespace(store=str(store.root), action="set",
                                      key="steward.not_a_key", value="1",
                                      json=False, value_two=None))
    assert "unknown setting" in str(exit_info.value)


def test_rpc_config_methods_use_the_same_writer(store, index):
    from intuition import rpc

    shown = rpc.dispatch(store, index, "config_show", {})
    keys = [row["key"] for row in shown["settings"]]
    assert "steward.idle_minutes" in keys and "safety.secure_enabled" in keys

    written = rpc.dispatch(store, index, "config_set",
                           {"key": "steward.idle_minutes", "value": "7"})
    assert written == {"key": "steward.idle_minutes", "value": 7}
    assert store.section("steward", "idle_minutes") == 7

    with pytest.raises(ConfigError):
        rpc.dispatch(store, index, "config_set", {"key": "nope", "value": "1"})


PACKAGE_DIR = Path(__file__).parent.parent / "src/intuition/adapters/pi_package"


def extension_source() -> str:
    return (Path(__file__).parent.parent
            / "src/intuition/adapters/pi_package/extensions/intuition/index.ts").read_text()


def test_pi_extension_configures_through_a_command_not_a_tool():
    """Config must not be model-callable: the keys include the safety knobs."""
    source = extension_source()

    assert 'registerCommand("intuition"' in source
    routes = source.split("const ROUTES: Record<string, Route> = {", 1)[1].split("\n};", 1)[0]
    assert "config" not in routes, "config must not be reachable from a tool route"

    from intuition.tools import MEMORY_TOOL_SCHEMAS

    assert not [name for name in MEMORY_TOOL_SCHEMAS if "config" in name]


def test_pi_package_manifests_agree_and_point_at_real_files():
    """The repo manifest is what a git or npm install reads; the nested one is
    what `intuition install pi` copies. Their shared fields must not drift."""
    root = json.loads((Path(__file__).parent.parent / "package.json").read_text())
    nested = json.loads((PACKAGE_DIR / "package.json").read_text())

    for field in ("name", "version", "description", "license", "keywords",
                  "peerDependencies", "repository", "homepage", "bugs"):
        assert root[field] == nested[field], field
    import tomllib

    pyproject = tomllib.loads((Path(__file__).parent.parent / "pyproject.toml").read_text())
    assert root["version"] == pyproject["project"]["version"], \
        "the Pi extension version has to track the package version"
    assert "pi-package" in root["keywords"]
    assert root["pi"]["extensions"] == [
        "./src/intuition/adapters/pi_package/extensions/intuition/index.ts"], \
        "the repo manifest has to point at the extension that exists"
    assert nested["pi"]["extensions"] == ["./extensions/intuition/index.ts"]
    for manifest, base in ((root, Path(__file__).parent.parent), (nested, PACKAGE_DIR)):
        for rel in manifest["pi"]["extensions"]:
            assert (base / rel).exists(), rel
    for rel in root["files"]:
        assert (Path(__file__).parent.parent / rel).exists(), rel


def test_the_shipped_tool_manifest_matches_the_schema():
    """The committed manifest is what a git install uses before any Python step,
    so it has to equal what the schema says."""
    from intuition.tools import host_manifest

    shipped = json.loads((PACKAGE_DIR / "extensions/intuition/tools.json").read_text())
    assert shipped == host_manifest(), (
        "regenerate with: python -c \"import json,pathlib;"
        "from intuition.tools import host_manifest;"
        "pathlib.Path('src/intuition/adapters/pi_package/extensions/intuition/tools.json')"
        ".write_text(json.dumps(host_manifest(), indent=1) + chr(10))\""
    )


def test_pi_extension_reports_a_broken_install_actionably():
    """A first-run failure has to say what to do. The behaviour these pin is
    captured end to end in the goal's degraded-paths.log."""
    source = extension_source()

    assert "cannot run the intuition CLI" in source
    assert "install it, then run" in source
    assert "this.proc = null" in source, \
        "a failed spawn must clear the handle, or later calls report a timeout"
    assert "this.lastStderr" in source, \
        "the server's own reason (missing store, bad config) has to reach the notice"
