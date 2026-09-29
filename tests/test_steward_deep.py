"""Deep pass jobs without a model: expiry, dedupe, profile, retention,
procedures promotion, reports (plan §6.3)."""

import os
import time

from intuition.model import Fact, Record
from intuition.steward.deep import (
    build_onepager,
    dedupe,
    expire,
    mark_raw_observed,
    promote_procedures,
    report,
    retention,
)


def _write_rec(store, rec: Record):
    store.write_record(rec)


def _rec(store, rid, rtype, name, facts=(), aliases=()):
    today = store.today()
    out = []
    for f in facts:
        conf = 0.9 if f[2] == "inferred" else None
        out.append(Fact(validity=f[0], text=f[1], trust=f[2],
                        sources=list(f[3]), confidence=conf))
    return Record(id=rid, type=rtype, name=name, aliases=list(aliases),
                  created=today, updated=today, facts=out)


def test_expires_until_facts(store):
    _write_rec(store, _rec(store, "topic-x", "topic", "X",
                           facts=[("until 2026-01-01", "old deal", "stated",
                                   ["raw:2025-12-01#1"]),
                                  ("since 2026-02", "new deal", "stated",
                                   ["raw:2026-02-01#1"])]))
    removed = expire(store)
    assert any("old deal" in r for r in removed)
    rec = store.load_record("topic-x")
    assert len(rec.facts) == 1 and "new deal" in rec.facts[0].text


def test_dedupe_merges_alias_overlap(store):
    _write_rec(store, _rec(store, "topic-alpha", "topic", "Alpha",
                           aliases=["proj alpha", "alpha initiative"],
                           facts=[("since 2026-01", "alpha fact", "stated", ["raw:2026-01-01#1"])]))
    _write_rec(store, _rec(store, "topic-beta", "topic", "Beta",
                           aliases=["proj alpha", "alpha initiative"],
                           facts=[("since 2026-02", "beta fact", "stated", ["raw:2026-02-01#1"])]))
    merged = dedupe(store)
    assert merged
    recs = store.scan_records()
    assert "topic-beta" not in recs
    keeper = recs["topic-alpha"]
    assert {f.text for f in keeper.facts} >= {"alpha fact", "beta fact"}
    assert "proj alpha" in keeper.aliases
    assert not store.resolve("shared/topic/topic-beta.md").exists()


def test_onepager_only_strong_facts(store):
    _write_rec(store, _rec(store, "pref-plain", "preference", "Plain English",
                           facts=[("since 2026-01", "answers in plain English",
                                   "stated", ["raw:2026-01-01#1"])]))
    _write_rec(store, _rec(store, "topic-ext", "topic", "Web says",
                           facts=[("since 2026-01", "external claim", "external",
                                   ["url:https://x.example"])]))
    _write_rec(store, _rec(store, "topic-weak", "topic", "Weak",
                           facts=[("since 2026-01", "a guess", "inferred",
                                   ["raw:2026-01-01#1"])]))
    _write_rec(store, _rec(store, "topic-strong", "topic", "Strong",
                           facts=[("since 2026-01", "a confirmed pattern", "inferred",
                                   ["raw:2026-01-01#1", "raw:2026-02-01#2"])]))
    build_onepager(store, None)
    text = store.read_text("shared/ONEPAGER.md")
    assert "plain English" in text                       # #stated enters (§9.1)
    assert "external claim" not in text                  # #external never enters
    assert "a guess" not in text                         # inferred without ≥2 sources out
    assert "a confirmed pattern" in text                 # strong inferred enters


def test_promote_procedures_seen_in_two_agents(store):
    store.write("agents/researcher/PROCEDURES.md",
                "# Procedures: researcher\n\n## Lessons\n- cite primary sources\n")
    store.write("agents/reviewer/PROCEDURES.md",
                "# Procedures: reviewer\n\n## Lessons\n- cite primary sources\n- check ECCN fields first\n")
    promoted = promote_procedures(store)
    assert len(promoted) == 1
    rec = store.load_record(promoted[0])
    assert rec.type == "procedure" and "cite primary" in rec.facts[0].text


def test_retention_deletes_old_raw_and_tasks(store):
    old_raw = store.dir("raw/2026-01-01.jsonl")
    old_raw.write_text('{"ts":"2026-01-01T00:00:00Z"}\n')
    past = time.time() - 40 * 86400
    os.utime(old_raw, (past, past))
    fresh_raw = store.dir("raw/2026-09-28.jsonl")
    fresh_raw.write_text('{"ts":"2026-09-28T00:00:00Z"}\n')
    old_task = store.dir("tasks/T-20260901-001")
    old_task.mkdir()
    (old_task / "brief.md").write_text("# brief\n")
    os.utime(old_task, (time.time() - 20 * 86400, time.time() - 20 * 86400))
    mark_raw_observed(store)                     # the observer consumed this capture
    removed = retention(store)
    assert removed["raw"] == 1 and removed["tasks"] == 1
    assert not old_raw.exists() and fresh_raw.exists()


def test_report_written(store):
    report(store, {"pass": "deep", "reason": "test", "light": {}}, seconds=0.1)
    p = store.dir(f"reports/{store.today()}.md")
    assert p.exists()
    text = p.read_text()
    assert "quarantine" in text and "misses" in text

