"""Steward scheduler state: the one owner of ``.intuition/state.json``.

Every key is written and read here, so no component reads a key that nothing
writes. The file lives under ``.intuition/``, which is gitignored, so it never
enters a commit and never affects the vault's bytes.

Keys:
  last_light      wall-clock of the last light pass
  last_deep       wall-clock of the last deep pass
  last_deep_day   local date of the last deep pass (the nightly window guard)
  last_activity   newest observed activity, advanced from raw capture mtimes
  raw_offsets     {"<raw file name>": bytes the observer has already consumed}

Raw progress is tracked per file. A single global byte offset cannot describe
several files: the sum of their sizes exceeds every individual file, so a
per-file comparison against it stops seeing new turns forever.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

STATE_DIR = ".intuition"
STATE_NAME = "state.json"

LAST_LIGHT = "last_light"
LAST_DEEP = "last_deep"
LAST_DEEP_DAY = "last_deep_day"
LAST_ACTIVITY = "last_activity"
RAW_OFFSETS = "raw_offsets"
ATTEMPTED_IDS = "attempted_ids"


class State:
    """Reads and writes the scheduler state file through named accessors."""

    def __init__(self, store):
        self.store = store
        self.path: Path = store.dir(f"{STATE_DIR}/{STATE_NAME}")
        self.data: dict = self._read()

    def _read(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            loaded = json.loads(self.path.read_text())
        except json.JSONDecodeError:
            return {}
        return loaded if isinstance(loaded, dict) else {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=1, sort_keys=True))

    # -- timestamps ---------------------------------------------------------

    def time_of(self, key: str) -> float:
        try:
            return float(self.data.get(key, 0) or 0)
        except (TypeError, ValueError):
            return 0.0

    def mark(self, key: str, when: float | None = None) -> None:
        self.data[key] = time.time() if when is None else when

    def note_activity(self, newest_mtime: float | None = None,
                      event: float | None = None) -> float:
        """Advance last_activity. Raw capture mtimes and a finished pass are both
        activity: the idle trigger measures quiet time since the last event, so a
        pass consumes its own idle condition instead of leaving it latched."""
        newest = max(self.time_of(LAST_ACTIVITY), newest_mtime or 0.0, event or 0.0)
        self.data[LAST_ACTIVITY] = newest
        return newest

    # -- plan attempts -----------------------------------------------------

    def attempted_ids(self) -> set[str]:
        """Inbox ids the last light pass planned. A rejected batch is not retried
        just because a session ended."""
        return {str(x) for x in (self.data.get(ATTEMPTED_IDS) or [])}

    def record_attempt(self, ids) -> None:
        self.data[ATTEMPTED_IDS] = sorted({str(x) for x in ids})

    # -- raw observer progress ---------------------------------------------

    def raw_offset(self, name: str) -> int:
        offsets = self.data.get(RAW_OFFSETS) or {}
        try:
            return int(offsets.get(name, 0))
        except (TypeError, ValueError):
            return 0

    def mark_raw_observed(self, offsets: dict[str, int] | None = None) -> dict[str, int]:
        """Record consumption.

        With *offsets*, store exactly those byte counts: what the observer read,
        not whatever is on disk after a model call has been running for a minute.
        Files the caller does not mention keep their previous offset.
        """
        merged = dict(self.data.get(RAW_OFFSETS) or {})
        if offsets is None:
            merged.update({p.name: p.stat().st_size for p in raw_files(self.store)})
        else:
            merged.update(offsets)
        self.data[RAW_OFFSETS] = merged
        return merged


def raw_files(store) -> list[Path]:
    """Raw capture files, oldest name first."""
    raw = store.dir("raw")
    return sorted(raw.glob("*.jsonl")) if raw.exists() else []


def pending_raw_bytes(store) -> int:
    """Bytes of raw capture the observer has not consumed (the light trigger)."""
    state = State(store)
    return sum(max(0, p.stat().st_size - state.raw_offset(p.name))
               for p in raw_files(store))


def newest_raw_mtime(store) -> float:
    """Newest mtime across raw capture files, or 0 when there is none."""
    return max((p.stat().st_mtime for p in raw_files(store)), default=0.0)


def load(store) -> State:
    return State(store)


def touch(store, *, light: bool = False, deep: bool = False) -> None:
    """Record that a pass finished, and refresh the activity signal."""
    state = State(store)
    now = time.time()
    if light:
        state.mark(LAST_LIGHT, now)
    if deep:
        state.mark(LAST_DEEP, now)
        state.data[LAST_DEEP_DAY] = store.today()
    state.note_activity(newest_raw_mtime(store),
                        event=now if (light or deep) else None)
    state.save()
