"""Promptness and trigger consumption (plan §6.1).

The state each case sets up is the state a quiet store is in right after a pass:
a recent deep run, activity recorded, and a batch below every threshold.
"""

import json
import time
from datetime import UTC, datetime, timedelta

from intuition import inbox
from intuition.steward import state as state_mod
from intuition.steward.tick import should_deep, should_light, tick

PENDING = inbox.PENDING


def _just_passed_state(store, **extra) -> None:
    """As if a deep pass finished a moment ago and nothing has happened since."""
    now = time.time()
    data = {"last_deep": now, "last_deep_day": store.today(), "last_activity": now}
    data.update(extra)
    store.dir(".intuition").mkdir(parents=True, exist_ok=True)
    store.dir(".intuition/state.json").write_text(json.dumps(data))


def _old_proposal(store, minutes: int, text: str = "a stalled proposal") -> dict:
    ts = datetime.now(UTC) - timedelta(minutes=minutes)
    item = {"id": f"old-{minutes}", "ts": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "host": "test", "session": "s-1", "agent": "main", "task_id": None,
            "kind": "fact", "text": text, "about": "pers-june", "source": "user",
            "evidence": text, "confidence": 0.9}
    store.append_jsonl(PENDING, item)
    return item


def test_session_end_commits_a_short_sessions_proposals(store, index):
    _just_passed_state(store)
    entry = inbox.append(store, kind="fact", source="user",
                         text="June runs the Thursday sync",
                         evidence="June runs the Thursday sync", about="pers-june")
    assert should_light(store)[0] is False, "below every threshold, as intended"

    result = tick(store, index, session_end=True)

    assert result["pass"] == "light"
    assert result["committed"], "the session's proposal is durable"
    assert entry["id"] not in {i["id"] for i in inbox.read_batch(store)}
    assert any("Thursday sync" in f.text
               for f in store.load_record("pers-june").facts)


def test_session_end_does_not_replan_an_attempted_batch(store, index, monkeypatch):
    from intuition.steward import light as light_mod

    _just_passed_state(store)
    entry = inbox.append(store, kind="fact", source="user", text="stuck proposal",
                         evidence="stuck proposal")
    monkeypatch.setattr(light_mod, "deterministic_plan", lambda *a, **k: {
        "ops": [{"op": "noop", "reason": "nothing to do yet"}], "summary": "noop"})

    first = tick(store, index, session_end=True)
    assert first["pass"] == "light" and first["unapplied"], "planned, nothing applied"
    assert entry["id"] in {i["id"] for i in inbox.read_batch(store)}

    second = tick(store, index, session_end=True)
    assert second["pass"] == "none", "an already-planned batch buys no second pass"


def test_session_end_fires_for_a_proposal_that_arrived_after_the_last_pass(store, index):
    _just_passed_state(store)
    inbox.append(store, kind="fact", source="user", text="first note",
                 evidence="first note", about="pers-june")
    assert tick(store, index, session_end=True)["pass"] == "light"

    _just_passed_state(store)
    inbox.append(store, kind="fact", source="user", text="second note",
                 evidence="second note", about="pers-june")
    later = tick(store, index, session_end=True)
    assert later["pass"] == "light", "a proposal made after the last pass is new"


def test_idle_trigger_is_consumed_by_its_own_pass(store, index):
    _just_passed_state(store, last_activity=time.time() - 31 * 60)
    fired, why = should_light(store)
    assert fired and "idle" in why

    tick(store, index, session_end=False)

    assert should_light(store)[0] is False, "the idle clock restarted with the pass"


def test_pending_age_condition_catches_a_stalled_proposal(store, index):
    _just_passed_state(store)
    _old_proposal(store, 45)
    fired, why = should_light(store)
    assert fired and "waiting" in why


def test_deep_runs_once_per_day_and_only_after_deep_time(store, index):
    _just_passed_state(store, last_deep=0, last_deep_day="")
    store.cfg.setdefault("steward", {})["deep_time"] = "00:00"
    assert should_deep(store)[0] is True

    later = (datetime.now() + timedelta(hours=1)).strftime("%H:%M")
    store.cfg["steward"]["deep_time"] = later if later > "00:01" else "23:59"
    assert should_deep(store)[0] is False, "the window has not opened yet"

    store.cfg["steward"]["deep_time"] = "00:00"
    tick(store, index, deep=True)
    assert should_deep(store)[0] is False, "one deep pass per local day"


def test_quiet_store_reports_no_trigger(store, index):
    _just_passed_state(store)
    result = tick(store, index)
    assert result["pass"] == "none" and result["skipped"] == "no trigger"


def test_explicit_note_waits_for_its_five_minutes(store, index):
    _just_passed_state(store)
    fresh = inbox.append(store, kind="fact", source="user", explicit=True,
                         text="remember the kicker deadline", evidence="kicker deadline")
    assert should_light(store)[0] is False, "a fresh explicit note is not urgent"

    aged = dict(fresh, ts=(datetime.now(UTC) - timedelta(minutes=6))
                .strftime("%Y-%m-%dT%H:%M:%SZ"))
    store.write(PENDING, json.dumps(aged) + "\n")
    fired, why = should_light(store)
    assert fired and "explicit" in why


def test_state_module_owns_every_key_it_reads(store):
    """No component reads a key nothing writes."""
    data = state_mod.State(store).data
    assert data == {}
    state_mod.touch(store, light=True, deep=True)
    written = state_mod.State(store).data
    assert set(written) <= {state_mod.LAST_LIGHT, state_mod.LAST_DEEP,
                            state_mod.LAST_DEEP_DAY, state_mod.LAST_ACTIVITY,
                            state_mod.RAW_OFFSETS, state_mod.ATTEMPTED_IDS}
    assert written[state_mod.LAST_DEEP_DAY] == store.today()
