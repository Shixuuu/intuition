"""Raw capture pipeline: the observer's per-file progress and retention.

Both defects these cover were live: a single global byte offset could never see
turns appended to a second file, and retention deleted capture the observer had
not read yet.
"""

import os
import time

from intuition.steward.deep import _unprocessed_raw, mark_raw_observed, retention
from intuition.steward.state import pending_raw_bytes


def test_observer_sees_turns_appended_after_a_pass(store, raw_capture):
    raw_capture(["the first turn mentions the launch"])
    assert "first turn" in _unprocessed_raw(store)

    mark_raw_observed(store)
    assert _unprocessed_raw(store) == "", "consumed capture is not re-sent"

    raw_capture(["a later turn mentions the vendor"])
    text = _unprocessed_raw(store)
    assert "later turn" in text, "turns appended after a pass are still unprocessed"
    assert "first turn" not in text


def test_observer_tracks_each_raw_file_separately(store, raw_capture):
    raw_capture(["monday turn"], day="2026-09-28")
    raw_capture(["tuesday turn"], day="2026-09-29")
    mark_raw_observed(store)
    assert _unprocessed_raw(store) == ""

    raw_capture(["appended to the older file"], day="2026-09-28")
    text = _unprocessed_raw(store)
    assert "appended to the older file" in text
    assert "monday turn" not in text and "tuesday turn" not in text


def test_pending_bytes_track_consumption_per_file(store, raw_capture):
    raw_capture(["alpha turn"], day="2026-09-28")
    raw_capture(["beta turn"], day="2026-09-29")
    before = pending_raw_bytes(store)
    assert before > 0

    mark_raw_observed(store)
    assert pending_raw_bytes(store) == 0

    raw_capture(["gamma turn"], day="2026-09-29")
    after = pending_raw_bytes(store)
    assert 0 < after < before, "only the appended bytes count as pending"


def test_retention_keeps_capture_the_observer_has_not_read(store, raw_capture):
    path = raw_capture(["capture nobody has observed"])
    old = time.time() - 40 * 86400
    os.utime(path, (old, old))
    assert store.section("retention", "raw_days") == 30

    removed = retention(store)
    assert path.exists(), "unobserved capture must survive retention"
    assert removed["kept_unobserved"] == 1 and removed["raw"] == 0

    mark_raw_observed(store)
    removed = retention(store)
    assert not path.exists() and removed["raw"] == 1


def test_retention_ignores_recent_capture(store, raw_capture):
    path = raw_capture(["today's capture"])
    mark_raw_observed(store)
    removed = retention(store)
    assert path.exists() and removed["raw"] == 0
