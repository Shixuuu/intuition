"""Search: FTS5 stemming, alias boost, as_of, one-hop, pending, budget (§5.2)."""

from intuition import search as search_mod


def _hits(store, index, queries, **kw):
    return search_mod.search(store, index, queries, **kw)


def _ids(hits):
    return [h.id for h in hits]


def test_stemming_plurals_and_word_forms(store, index):
    # the three instinct-memory failures: meeting→meetings, migrating→migration,
    # and "pi" must NOT match inside "api" (plan §5.2)
    assert _ids(_hits(store, index, ["meetings"])) == []
    store.write("shared/topic/t-sync.md", """\
---
id: topic-sync
type: topic
name: Sync meetings
created: 2026-09-01
updated: 2026-09-01
---
## Facts
- (since 2026-09) Thursday sync with the team. #stated ^raw:2026-09-01#1
""")
    index.update()
    assert "topic-sync" in _ids(_hits(store, index, ["meeting"]))
    assert "topic-sync" in _ids(_hits(store, index, ["meetings"]))


def test_substring_does_not_match(store, index):
    ids = _ids(_hits(store, index, ["pi"]))
    assert all(i != "pers-june" for i in ids)      # no "api" substring hits at all
    assert ids == [] or all(h.score for h in _hits(store, index, ["pi"]))


def test_exact_alias_beats_everything(store, index):
    hits = _hits(store, index, ["jj"])
    assert hits and hits[0].id == "pers-june" and hits[0].via == "alias"


def test_multi_query_fusion_rrf(store, index):
    hits = _hits(store, index, ["who is the PM for Lighthouse", "june product"])
    assert hits[0].id == "pers-june"


def test_as_of_uses_validity(store, index):
    # May: Beacon fact, not Lighthouse; Sep: Lighthouse, not Beacon (temporal)
    may = _hits(store, index, ["june pm"], as_of="2026-05-15")
    may_text = "\n".join(may[0].fact_lines)
    assert "Beacon" in may_text and "Lighthouse" not in may_text
    june = store.load_record("pers-june")
    lines = "\n".join(june.fact_lines(as_of="2026-05-15"))
    assert "Beacon" in lines and "Lighthouse" not in lines
    sep = "\n".join(june.fact_lines(as_of="2026-09-15"))
    assert "Lighthouse" in sep and "Beacon" not in sep


def test_one_hop_expansion(store, index):
    # "launch project" only matches ws-lighthouse (its alias + facts);
    # june should arrive through the manages link at decayed score.
    hits = _hits(store, index, ["launch project"])
    ids = _ids(hits)
    assert "ws-lighthouse" in ids
    assert any(h.via.startswith("hop") for h in hits)


def test_pending_inbox_searchable_immediately(store, index):
    from intuition import inbox
    inbox.append(store, kind="preference", text="June prefers agendas the day before",
                 source="user", evidence="send june the agenda the day before, she likes that",
                 about="pers-june")
    hits = _hits(store, index, ["june agenda"])
    pend = [h for h in hits if h.pending]
    assert pend, "fresh note must be searchable before any Steward run (D3)"
    assert "(pending" in search_mod.render_hits([pend[0]])


def test_miss_logging_and_alias_candidate(store, index):
    _hits(store, index, ["caffeine policy"])
    zero, total = index.miss_rate()
    assert zero == 1 and total == 1
    index.log_hit_after_miss("caffeine policy", "pers-june")
    cands = index.alias_candidates()
    assert ("caffeine policy", "pers-june", 1) in cands


def test_budget_caps_injection(store, index):
    for i in range(8):
        store.write(f"shared/topic/t-{i}.md", f"""\
---
id: topic-{i}
type: topic
name: Topic {i}
created: 2026-09-01
updated: 2026-09-01
---
## Facts
- (since 2026-09) topic {i} filler commonword. #stated ^raw:2026-09-01#1
""")
    index.update()
    hits = search_mod.budget(store, _hits(store, index, ["topic filler commonword"], limit=10))
    assert len(hits) <= int(store.section("search", "max_records", 4))


def test_memory_tools_work_from_a_host_thread(store, index):
    """A host dispatches memory tool calls on its own worker threads, while the
    query connection was built on the caller's. Includes a type-filtered query,
    which reads the record types through the same connection."""
    import threading

    from intuition.tools import handle_tool_call

    outcomes: list = []
    errors: list = []

    def search(query, **extra):
        try:
            outcomes.append(handle_tool_call(
                store, index, "memory_search", {"queries": [query], **extra}))
        except Exception as exc:                 # noqa: BLE001 - reported below
            errors.append(repr(exc))

    def read():
        try:
            outcomes.append(handle_tool_call(
                store, index, "memory_read", {"id_or_name": "pers-june"}))
        except Exception as exc:                 # noqa: BLE001 - reported below
            errors.append(repr(exc))

    workers = [
        threading.Thread(target=search, args=("who is the pm for lighthouse",)),
        threading.Thread(target=search, args=("june lighthouse",), kwargs={"type": "person"}),
        threading.Thread(target=read),
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()

    assert not errors, errors
    assert any(r["id"] == "pers-june"
               for outcome in outcomes for r in outcome.get("records", []))
    typed = [outcome for outcome in outcomes if outcome.get("records")
             and all(r["id"].startswith("pers-") for r in outcome["records"])]
    assert typed, "the type-filtered query must come back holding only people"
    assert any(outcome.get("id") == "pers-june" for outcome in outcomes)
    index.record_usage("pers-june")               # write path from this thread too


def test_retrieval_eval_gate(store, index):
    """plan §11.2: run the eval file against the sample vault, all cases hit."""
    import tomllib
    from pathlib import Path
    cases = tomllib.loads(
        (Path(__file__).parent.parent / "evals" / "retrieval.toml").read_text())["case"]
    hits3 = 0
    for case in cases:
        out = search_mod.search(store, index, [case["query"]], limit=8)
        if any(i in set(case["expect"]) for i in _ids(out)[:3]):
            hits3 += 1
    assert hits3 == len(cases), f"hit@3 = {hits3}/{len(cases)}"
