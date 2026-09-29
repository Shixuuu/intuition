"""One source per schema surface, and budgets that are named for their unit.

Both hosts read the tool schemas from ``tools.MEMORY_TOOL_SCHEMAS``: the Hermes
adapter imports it and the Pi installer projects it into a manifest. The
character budgets replaced a set of ``*_tokens`` keys whose values the code
multiplied by four, and one of them (the profile cap) was never applied at all.
"""

import argparse
import json

from intuition import context
from intuition.cli import cmd_install_pi
from intuition.tools import MEMORY_TOOL_SCHEMAS, TOOL_ORDER, schemas_for


def test_schema_source_covers_every_tool_once():
    assert set(TOOL_ORDER) == set(MEMORY_TOOL_SCHEMAS)
    assert len(TOOL_ORDER) == len(set(TOOL_ORDER))
    for name, spec in MEMORY_TOOL_SCHEMAS.items():
        assert spec["description"], name
        assert spec["parameters"].get("type") == "object", name


def test_every_schema_property_declares_a_type():
    """A provider rejects a property that only carries an enum: Moonshot's
    validator needs `type` on every property, and a real Pi session failed on it."""
    def walk(node, path):
        if not isinstance(node, dict):
            return
        if node.get("type") == "object" or "properties" in node:
            for name, prop in (node.get("properties") or {}).items():
                assert "type" in prop or "anyOf" in prop, f"{path}.{name} has no type"
                walk(prop, f"{path}.{name}")
        if node.get("type") == "array":
            walk(node.get("items"), f"{path}[]")

    for name, spec in MEMORY_TOOL_SCHEMAS.items():
        walk(spec["parameters"], name)


def test_roles_get_the_tools_they_may_call():
    assert {s["name"] for s in schemas_for("subagent")} == {"memory_search", "memory_read"}
    assert [s["name"] for s in schemas_for("main")] == list(TOOL_ORDER)
    assert schemas_for("cron") == []


def test_hermes_adapter_reads_the_same_source():
    from intuition.adapters import hermes as hermes_mod

    provider = hermes_mod.IntuitionProvider()
    assert provider.get_tool_schemas() == schemas_for("main")
    provider._role = "subagent"
    assert provider.get_tool_schemas() == schemas_for("subagent")
    provider._role = "cron"
    assert provider.get_tool_schemas() == []


def test_pi_install_writes_the_schema_manifest(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("shutil.which", lambda name: None)      # stay hermetic
    cmd_install_pi(argparse.Namespace(pi_home=str(tmp_path)))

    target = tmp_path / "intuition"
    manifest = json.loads(
        (target / "extensions" / "intuition" / "tools.json").read_text())
    assert manifest == {"main": schemas_for("main"), "subagent": schemas_for("subagent")}
    assert (target / "package.json").exists()
    assert (target / "extensions" / "intuition" / "index.ts").exists()
    assert f"pi install {target}" in capsys.readouterr().out


def test_profile_block_is_capped(store, index):
    body = "\n".join(f"- line {n}" for n in range(2000))
    store.write("shared/PROFILE.md", body)
    store.cfg.setdefault("profile", {})["pinned_max_chars"] = 200

    block = context.build_main_prefix(store, index)

    assert "truncated at profile.pinned_max_chars = 200 chars" in block
    assert len(block) < len(body)


def test_budget_keys_name_their_unit():
    from intuition.store import DEFAULT_CONFIG

    assert "_tokens" not in DEFAULT_CONFIG
    for key in ("pinned_max_chars", "generated_max_chars", "prefix_max_chars",
                "observer_raw_chars", "reflector_log_chars", "light_raw_chars"):
        assert key in DEFAULT_CONFIG
