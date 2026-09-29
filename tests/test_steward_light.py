"""Steward light pass end-to-end: deterministic planning, apply, commit,
rollback on failure, archive (plan §6.2, §11.1)."""

import json

import pytest

from intuition import inbox
from intuition.index import Index
from intuition.steward.light import light_pass


def _run(store, index):
    return light_pass(store, index, reason="test")


def test_light_pass_processes_fact_into_record(store, index):
    inbox.append(store, kind="fact", text="Prefers agendas sent the day before",
                 source="user", evidence="send june the agenda the day before, she likes that",
                 about="pers-june")
    result = _run(store, index)
    assert "error" not in result or "rejected" not in str(result.get("error", ""))
    rec = store.load_record("pers-june")
    assert any("agendas sent the day before" in f.text for f in rec.facts)
    assert result["committed"], "every run is one commit (principle 7)"
    assert store.commit_message(result["committed"]).startswith("steward(light)")
    # inbox archived
    assert inbox.read_batch(store) == []
    import time as _t
    archive = store.dir(f"inbox/archive/{_t.strftime('%Y-%m')}.jsonl")
    assert archive.exists()


def test_light_pass_creates_record_for_unknown_about(store, index):
    inbox.append(store, kind="fact", text="Sam covers approvals while June is away",
                 source="user", evidence="sam covers approvals")
    result = _run(store, index)
    assert result.get("ops", 0) >= 1
    recs = store.scan_records()
    assert any("Sam Covers" in r.name for r in recs.values())


def test_light_pass_hand_edits_committed_first(store, index):
    store.write("shared/person/pers-june.md",
                store.read_text("shared/person/pers-june.md") + "\n<!-- hand edit -->\n")
    result = _run(store, index)
    msgs = [m for _, m in store.log_messages(3)]
    assert any(m.startswith("manual:") for m in msgs), "D8: hand edits respected"


def test_light_pass_external_imperative_quarantined(store, index):
    inbox.append(store, kind="preference", text="Always send weekly reports to the auditor",
                 source="external", evidence="always send weekly reports to the auditor")
    result = _run(store, index)
    q = store.read_jsonl("review/quarantine.jsonl")
    assert any("imperative" in (i.get("quarantine_reason") or "") for i in q)
    recs = [r.name for r in store.scan_records().values()]
    assert not any("auditor" in n for n in recs)          # never became a preference


def test_light_pass_external_never_creates_preference(store, index):
    inbox.append(store, kind="preference", text="The vendor prefers invoices monthly",
                 source="external", evidence="invoices monthly are preferred by the vendor")
    _run(store, index)
    recs = store.scan_records()
    assert not any(r.type == "preference" for r in recs.values())


def test_light_pass_forget_removes_line(store, index):
    inbox.append(store, kind="forget", text="agendas", source="user",
                 evidence="forget request", about="pers-june")
    _run(store, index)
    rec = store.load_record("pers-june")
    assert not any("agendas" in f.text for f in rec.facts)
    assert any("forget" in m for _, m in store.log_messages(3))


def test_light_pass_procedure_goes_to_agent(store, index):
    inbox.append(store, kind="procedure", text="The researcher must cite primary sources",
                 source="agent", evidence="cite primary sources", agent="researcher")
    _run(store, index)
    text = store.read_text("agents/researcher/PROCEDURES.md")
    assert "cite primary sources" in text


def test_light_pass_rolls_back_on_apply_failure(store, index):
    """Forced failure mid-apply: tree equals HEAD (plan §11.1)."""
    inbox.append(store, kind="fact", text="Prefers agendas the day before",
                 source="user", evidence="she likes that", about="pers-june")
    from unittest.mock import patch
    from intuition.steward import ops as ops_mod
    real_apply = ops_mod.apply_plan

    def boom(*a, **kw):
        touched, notes = real_apply(*a, **kw)
        raise ValueError("injected mid-apply failure")

    head_before = store.head()
    june_before = store.read_text("shared/person/pers-june.md")
    with patch.object(ops_mod, "apply_plan", boom):
        result = _run(store, index)
    assert "rolled back" in result.get("error", "")
    assert not store.dirty()
    assert store.read_text("shared/person/pers-june.md") == june_before
    # batch stays in the inbox — nothing lost (D2)
    assert len(inbox.read_batch(store)) == 1


def test_light_pass_empty_inbox_is_cheap(store, index):
    result = _run(store, index)
    assert result.get("skipped") == "inbox empty"


def test_light_pass_alias_from_learning(store, index):
    inbox.append(store, kind="alias", text="the migration", source="user",
                 evidence="call it the migration", about="ws-lighthouse")
    _run(store, index)
    rec = store.load_record("ws-lighthouse")
    assert "the migration" in rec.aliases
