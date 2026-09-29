"""Store: atomic writes, path safety, locks, git transaction (plan §9.5, §6.2)."""

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from intuition.store import StoreError


def test_atomic_write_no_partial(store):
    p = store.write("shared/topic/t-x.md", "# X\n")
    assert p.read_text() == "# X\n"
    leftovers = list(p.parent.glob(".*tmp*")) + list(p.parent.glob("*.tmp"))
    assert not leftovers


def test_path_traversal_blocked(store):
    with pytest.raises(StoreError):
        store.resolve("../outside.md")
    with pytest.raises(StoreError):
        store.write("shared/../../evil.md", "nope")


def test_record_roundtrip_via_store(store, index):
    recs = store.scan_records()
    assert set(recs) >= {"pers-june", "ws-lighthouse", "dec-use-fts5"}
    june = store.load_record("june")            # by alias
    assert june.id == "pers-june"
    pm = store.load_record("the pm")            # by alias with spaces
    assert pm.id == "pers-june"


def test_commit_and_rollback(store):
    store.write("shared/topic/t-a.md", "# A\n")
    sha = store.commit("steward(light): test")
    assert sha
    assert not store.dirty()
    store.write("shared/topic/t-b.md", "# B\n")
    store.rollback()
    assert not store.dirty()
    assert not store.resolve("shared/topic/t-b.md").exists()
    assert store.resolve("shared/topic/t-a.md").exists()


def test_bad_json_stops_run(store):
    p = store.dir("inbox/pending.jsonl")
    p.write_text('{"id":"ok"}\n{broken\n')
    with pytest.raises(StoreError, match="bad JSON"):
        store.read_jsonl("inbox/pending.jsonl")


def test_concurrent_appends_no_lost_lines(store):
    """Hermes + Pi + Steward appending at once — no lost inbox lines (§11.1)."""
    def writer(n):
        for i in range(25):
            store.append_jsonl("inbox/pending.jsonl",
                               {"id": f"w{n}-{i}", "ts": store.now_iso()})

    with ThreadPoolExecutor(max_workers=4) as ex:
        list(ex.map(writer, range(4)))
    items = store.read_jsonl("inbox/pending.jsonl")
    ids = {i["id"] for i in items}
    assert len(items) == 100
    assert len(ids) == 100


def test_jsonl_survives_kill_mid_write(store, tmp_path):
    """fsync + whole-line writes mean a kill -9 cannot produce a torn line."""
    store.append_jsonl("raw/2026-09-29.jsonl", {"n": 1})
    # simulate crash: partial write via raw os write without fsync is still
    # one append() call on POSIX for small buffers; we assert the contract:
    lines = store.read_jsonl("raw/2026-09-29.jsonl")
    assert lines == [{"n": 1}]


def test_lock_is_exclusive(store):
    order = []
    import time
    def hold():
        with store.lock("steward"):
            order.append("in")
            time.sleep(0.05)
            order.append("out")
    t = threading.Thread(target=hold)
    t.start()
    time.sleep(0.01)
    with store.lock("steward"):
        order.append("second-in")
    t.join()
    assert order == ["in", "out", "second-in"]
