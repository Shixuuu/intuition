"""Transaction integrity: a fault anywhere in a Steward run leaves the vault at
HEAD and the process's own view of the vault unchanged (plan §6.2 step 8, §6.3).

Each case injects the fault into the real path after it has already mutated
records, so a rollback that only fixes the files but not the in-process record
cache would still fail here.
"""

import pytest

from intuition import inbox
from intuition.steward import deep as deep_mod
from intuition.steward import ops as ops_mod
from intuition.steward.deep import deep_pass
from intuition.steward.light import light_pass


def _run(store, index) -> dict:
    return light_pass(store, index, reason="transaction")


def test_light_fault_after_apply_leaves_no_phantom_facts(store, index):
    inbox.append(store, kind="fact", source="user", text="June runs the sync",
                 evidence="June runs the sync", about="pers-june")
    before = store.read_text("shared/person/pers-june.md")
    real_apply = ops_mod.apply_plan

    def boom(*a, **kw):
        real_apply(*a, **kw)                   # real mutations, then a fault
        raise ValueError("injected mid-apply failure")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(ops_mod, "apply_plan", boom)
        result = _run(store, index)

    assert "rolled back" in result["error"]
    assert store.read_text("shared/person/pers-june.md") == before
    assert not store.git("status", "--porcelain", "--", "shared").strip(), \
        "the vault is back at HEAD"
    assert "inbox empty" not in result.get("skipped", "")
    assert inbox.read_batch(store), "the batch is still pending"

    # and the next run must still work: a rollback must not remove directories
    # or leave a pathspec that the next hand-edit sweep cannot stage
    follow_up = _run(store, index)
    assert "error" not in follow_up, follow_up.get("error")
    assert follow_up["committed"], "the retry applies the same proposal"


def test_light_fault_keeps_the_process_view_of_the_vault(store, index):
    """The failed plan's mutations must not survive in the record cache."""
    inbox.append(store, kind="fact", source="user", text="June runs the sync",
                 evidence="June runs the sync", about="pers-june")
    phantom = "PHANTOM fact that must never be visible"
    before = store.load_record("pers-june")
    real_apply = ops_mod.apply_plan

    def boom(store_, plan, batch):
        plan = dict(plan)
        plan["ops"] = list(plan.get("ops", [])) + [
            {"op": "add_fact", "id": "pers-june", "validity": "since 2026-09",
             "text": phantom, "trust": "stated", "sources": ["inbox:x"],
             "evidence": "x"}]
        touched, notes = real_apply(store_, plan, batch)
        raise ValueError("injected after a real add_fact")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(ops_mod, "apply_plan", boom)
        result = _run(store, index)

    assert "rolled back" in result["error"]
    served = "\n".join(store.load_record("pers-june").fact_lines(store.today()))
    assert phantom not in served, "the process must not serve rolled-back facts"
    assert len(store.load_record("pers-june").facts) == len(before.facts)
    assert phantom not in store.read_text("shared/person/pers-june.md")
    assert not store.git("status", "--porcelain", "--", "shared").strip()


def test_deep_job_fault_rolls_back_every_job(store, index):
    """expire() succeeds and writes; a later job fails; both are undone."""
    store.write("shared/topic/topic-expiring.md", """\
---
id: topic-expiring
type: topic
name: Expiring topic
created: 2026-01-01
updated: 2026-01-01
---
# Expiring topic

## Facts
- (until 2026-02-01) This fact has expired. #stated ^raw:2026-01-01#1
""")
    store.commit("seed expiring record", add_all=True)
    head = store.head()
    before = store.read_text("shared/topic/topic-expiring.md")

    def boom(*a, **kw):
        raise ValueError("injected mid-deep-jobs failure")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(deep_mod, "build_onepager", boom)
        result = deep_pass(store, index, reason="transaction")

    assert "rolled back" in result["error"]
    assert store.head() == head, "no commit was made"
    assert store.read_text("shared/topic/topic-expiring.md") == before, \
        "expire()'s deletion was undone with the other jobs"
    assert not store.dirty(), "the process's view matches HEAD"
    rec = store.load_record("topic-expiring")
    assert [f for f in rec.facts if "expired" in f.text], "the fact is back"


def test_run_commits_its_own_archive_in_one_commit(store, index):
    """One light run is one commit, including the inbox rewrite."""
    inbox.append(store, kind="fact", source="user", text="June runs the sync",
                 evidence="June runs the sync", about="pers-june")
    commits_before = int(store.git("rev-list", "--count", "HEAD").strip())

    result = _run(store, index)

    commits_after = int(store.git("rev-list", "--count", "HEAD").strip())
    assert commits_after == commits_before + 1, "exactly one commit"
    assert result["committed"]
    assert not store.dirty(), "the archive rewrite rode along in that commit"
    assert inbox.read_batch(store) == []
