"""Steward triggers (plan §6.1): light on activity signals, deep once per night
after deep_time. A lock file ensures a single writer.

Every trigger is a condition over the store's own state, and each one is
consumed by the pass it fires: the idle clock restarts on any pass, the nightly
window closes for the local day, and a session-end pass remembers which
proposals it already planned.
"""

from __future__ import annotations

import time

from ..model import parse_iso_ts
from .deep import deep_pass
from .light import light_pass
from .state import (
    LAST_ACTIVITY,
    LAST_DEEP,
    LAST_DEEP_DAY,
    State,
    newest_raw_mtime,
    pending_raw_bytes,
)

STEWARDSHIP_LOCK = "steward"


def should_light(store) -> tuple[bool, str]:
    """plan §6.1 light triggers: any one fires."""
    from .. import inbox as inbox_mod
    batch = inbox_mod.read_batch(store)
    if len(batch) >= int(store.section("steward", "light_inbox_items", 10)):
        return True, f"{len(batch)} inbox items"
    for item in batch:
        if item.get("explicit") and _age(item.get("ts", "")) > 300:
            return True, "explicit note older than 5 min"
    pending = pending_raw_bytes(store)
    if pending >= int(store.section("steward", "light_raw_chars", 80000)):
        return True, f"{pending} unobserved raw chars"
    age_minutes = int(store.section("steward", "pending_age_minutes", 20))
    oldest = inbox_mod.oldest_age_seconds(store) / 60.0
    if batch and age_minutes and oldest >= age_minutes:
        return True, f"oldest proposal waiting {int(oldest)} min"
    idle_min = _idle_minutes(store)
    if 0 < idle_min and idle_min >= int(store.section("steward", "idle_minutes", 30)):
        return True, f"idle {int(idle_min)} min after activity"
    return False, ""


def should_light_at_session_end(store) -> tuple[bool, str]:
    """A session ended holding proposals the last pass never planned.

    The condition is the pending set against the last attempt, so a batch that
    was already planned and rejected does not buy a second model call.
    """
    from .. import inbox as inbox_mod
    pending = {item["id"] for item in inbox_mod.read_batch(store)}
    if not pending:
        return False, ""
    fresh = pending - State(store).attempted_ids()
    if not fresh:
        return False, ""
    return True, f"session end with {len(fresh)} unplanned proposals"


def should_deep(store) -> tuple[bool, str]:
    """Once per local day, after deep_time, and only when something happened."""
    deep_time = str(store.section("steward", "deep_time", "03:00"))
    state = State(store)
    if state.data.get(LAST_DEEP_DAY) == store.today():
        return False, ""
    if time.strftime("%H:%M") < deep_time:
        return False, ""
    # raw capture written since the last pass counts as activity too
    activity = max(state.time_of(LAST_ACTIVITY), newest_raw_mtime(store))
    if state.time_of(LAST_DEEP) >= activity > 0:
        return False, ""
    return True, f"past deep_time {deep_time}"


def tick(store, index, *, light: bool = False, deep: bool = False,
         session_end: bool = False, reason: str = "") -> dict:
    """Entry point for `intuition tick` and for the host session-end hooks."""
    with store.lock(STEWARDSHIP_LOCK):
        if deep:
            return deep_pass(store, index, reason=reason or "forced")
        if light:
            return light_pass(store, index, reason=reason or "forced")
        do_light, why = should_light(store)
        if not do_light and session_end:
            do_light, why = should_light_at_session_end(store)
        if do_light:
            return light_pass(store, index, reason=why)
        do_deep, why = should_deep(store)
        if do_deep:
            return deep_pass(store, index, reason=why)
        return {"pass": "none", "skipped": "no trigger", "reason": ""}


# -- helpers ------------------------------------------------------------------------

def _idle_minutes(store) -> float:
    """Quiet minutes: the last pass, or the newest raw capture, whichever is later.

    Raw written between passes counts as activity even though the state file is
    only touched at the end of a pass, so idle cannot fire mid-session.
    """
    last = max(State(store).time_of(LAST_ACTIVITY), newest_raw_mtime(store))
    if not last:
        return 0.0
    return (time.time() - last) / 60.0


def _age(ts: str) -> float:
    return time.time() - parse_iso_ts(ts)
