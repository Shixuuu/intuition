"""Grammar round-trip + validity logic."""

from intuition.model import (
    Fact,
    Link,
    Record,
    close_fact,
    make_id,
    parse_record,
    render_record,
    validity_contains,
)


def _june_record() -> Record:
    return parse_record("""\
---
id: pers-june
type: person
name: June
aliases: [june, jj, the pm]
created: 2026-08-02
updated: 2026-09-26
---
# June

PM on Lighthouse.

## Facts
- (since 2026-08) PM for Lighthouse. #stated ^raw:2026-08-02#14
- (2026-03 → 2026-08) PM for Beacon. #stated ^raw:2026-03-02#7
- (until 2026-10-15) On leave; Sam covers approvals. #stated ^inbox:01J9X2
- (2026-09-26) Prefers agendas the day before. #inferred:0.7 ^obs:2026-09#41

## Links
- manages [[ws-lighthouse]] (since 2026-08)
- works-at [[org-tidewater-labs]]
""")


def test_round_trip():
    rec = _june_record()
    again = parse_record(render_record(rec))
    assert render_record(again) == render_record(rec)
    assert again.id == "pers-june"
    assert again.aliases == ["june", "jj", "the pm"]
    assert len(again.facts) == 4
    assert [(link.rel, link.target) for link in again.links] == [
        ("manages", "ws-lighthouse"), ("works-at", "org-tidewater-labs")]


def test_fact_fields_round_trip():
    rec = _june_record()
    f = rec.facts[0]
    assert f.validity == "since 2026-08"
    assert f.trust == "stated"
    assert f.sources == ["raw:2026-08-02#14"]
    inf = rec.facts[3]
    assert inf.trust == "inferred" and inf.confidence == 0.7


def test_validity_current():
    # current: today inside the range
    assert validity_contains("since 2026-08", "2026-09-29")
    assert not validity_contains("since 2026-08", "2026-07-15")
    assert validity_contains("2026-03 → 2026-08", "2026-05-01")
    assert not validity_contains("2026-03 → 2026-08", "2026-09-01")
    assert validity_contains("until 2026-10-15", "2026-09-29")
    assert not validity_contains("until 2026-10-15", "2026-10-16")
    assert validity_contains("2026-09-26", "2027-01-01")    # point events never expire


def test_validity_as_of():
    # "Who was Beacon's PM in May?" must use validity, not latest
    rec = _june_record()
    may_pm = [f for f in rec.facts if f.at("2026-05-15") and "PM for" in f.text]
    assert len(may_pm) == 1 and "Beacon" in may_pm[0].text
    sep_pm = [f for f in rec.facts if f.at("2026-09-15") and "PM for" in f.text]
    assert len(sep_pm) == 1 and "Lighthouse" in sep_pm[0].text
    # the leave fact also covers May (correct: it started before May)
    assert any(f.at("2026-05-15") and "leave" in f.text for f in rec.facts)


def test_close_fact_closes_not_deletes():
    rec = _june_record()
    assert close_fact(rec, "PM for Lighthouse", "2026-09", ["raw:2026-09-29#3"])
    f = rec.facts[0]
    assert f.validity == "2026-08 → 2026-09"
    # old wording still in the file (git holds the history; the line holds the range)
    assert "PM for Lighthouse" in render_record(rec)
    assert not close_fact(rec, "PM for Mars", "2026-09", [])


def test_ids_and_types():
    assert make_id("person", "June Tan!") == "pers-june-tan"
    assert make_id("workstream", "Lighthouse") == "ws-lighthouse"
    assert make_id("decision", "Use FTS5") == "dec-use-fts5"


def test_decision_record_round_trip():
    text = """\
---
id: dec-use-fts5
type: decision
name: Use SQLite FTS5 for search
aliases: [fts5, no vectors]
status: accepted
decided: 2026-09-29
decided_by: user
created: 2026-09-29
updated: 2026-09-29
---
## Decision
Use SQLite FTS5 with porter stemming.
## Why
Stdlib, fast.
## Revisit if
Miss rate > 10%.
"""
    rec = parse_record(text)
    assert rec.decision["Decision"] == ["Use SQLite FTS5 with porter stemming."]
    assert rec.front["status"] == "accepted"
    assert render_record(parse_record(render_record(rec))) == render_record(rec)


def test_fact_dataclass_direct():
    f = Fact("since 2026-01", "x", "stated", ["raw:2026-01-01#1"])
    assert f.current("2026-06-01")
    assert f.line() == "- (since 2026-01) x #stated ^raw:2026-01-01#1"
    link = Link("manages", "ws-x", "since 2026-01")
    assert link.line() == "- manages [[ws-x]] (since 2026-01)"
