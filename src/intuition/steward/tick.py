"""Steward triggers (plan §6.1): light every 15 min on activity signals,
deep once per night after deep_time. A lock file ensures a single writer.
"""

from __future__ import annotations

import json
import time

from .deep import deep_pass
from .light import _touch_state, light_pass

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
    new_chars = _raw_chars_since(store)
    if new_chars >= int(store.section("steward", "light_raw_tokens", 20000)) * 4:
        return True, f"{new_chars // 4} raw tokens"
    idle_min = _idle_minutes(store)
    if 0 < idle_min and idle_min >= int(store.section("steward", "idle_minutes", 30)):
        return True, f"idle {int(idle_min)} min after activity"
    return False, ""


def should_deep(store) -> tuple[bool, str]:
    deep_time = str(store.section("steward", "deep_time", "03:00"))
    now = time.strftime("%H:%M")
    state = _state(store)
    last_deep = state.get("last_deep_day", "")
    today = store.today()
    if last_deep == today:
        return False, ""
    if _state(store).get("last_deep", 0) and not _activity_since(store, deep_time):
        return False, ""
    # after deep_time (handles crossing midnight loosely: fire from deep_time on)
    if now >= deep_time or now < "04:00":
        return True, f"past deep_time {deep_time}"
    return False, ""


def tick(store, index, *, light: bool = False, deep: bool = False,
         reason: str = "") -> dict:
    """Entry point for `intuition tick`. Holds the global steward lock."""
    with store.lock(STEWARDSHIP_LOCK):
        if deep:
            return deep_pass(store, index, reason=reason or "forced")
        if light:
            return light_pass(store, index, reason=reason or "forced")
        do_light, why = should_light(store)
        if do_light:
            return light_pass(store, index, reason=why)
        do_deep, why = should_deep(store)
        if do_deep:
            return deep_pass(store, index, reason=why)
        return {"pass": "none", "skipped": "no trigger", "reason": ""}


# -- helpers ------------------------------------------------------------------------

def _state(store) -> dict:
    path = store.dir(".intuition/state.json")
    if path.exists():
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            pass
    return {}


def _raw_chars_since(store) -> int:
    state = _state(store)
    done = state.get("raw_done_bytes", 0)
    total = 0
    raw = store.dir("raw")
    for p in raw.glob("*.jsonl") if raw.exists() else ():
        total += p.stat().st_size
    return max(0, total - done)


def _idle_minutes(store) -> float:
    state = _state(store)
    last = state.get("last_activity", 0)
    if not last:
        return 0.0
    return (time.time() - last) / 60.0


def _activity_since(store, deep_time: str) -> bool:
    state = _state(store)
    last_deep = state.get("last_deep", 0)
    last_act = state.get("last_activity", 0)
    return last_act > last_deep


def _age(ts: str) -> float:
    try:
        import calendar
        return time.time() - calendar.timegm(
            time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ"))
    except (ValueError, TypeError):
        return 0.0
