"""RPC server end-to-end over stdio (plan §8.2) and triggers (plan §6.1)."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from intuition import inbox

SRC = str(Path(__file__).parent.parent / "src")


class RpcClient:
    def __init__(self, store_path):
        env = {**dict(os.environ), "INTUITION_STORE": str(store_path),
               "PYTHONPATH": SRC}
        self.p = subprocess.Popen(
            [sys.executable, "-m", "intuition.rpc"], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, text=True)
        self.nid = 0

    def call(self, method, params=None):
        self.nid += 1
        self.p.stdin.write(json.dumps({"id": self.nid, "method": method,
                                       "params": params or {}}) + "\n")
        self.p.stdin.flush()
        line = self.p.stdout.readline()
        assert line, self.p.stderr.read()
        return json.loads(line)

    def close(self):
        self.p.stdin.close()
        self.p.wait(timeout=5)


@pytest.fixture
def rpc(store):
    client = RpcClient(store.root)
    yield client
    client.close()


def test_rpc_ping(rpc):
    out = rpc.call("ping")
    assert out["result"]["pong"] is True


def test_rpc_search_and_read(rpc):
    out = rpc.call("search", {"queries": ["who is the pm for lighthouse"]})
    records = out["result"]["records"]
    assert any(r["id"] == "pers-june" for r in records)
    out = rpc.call("read", {"id_or_name": "jj"})
    assert out["result"]["id"] == "pers-june"


def test_rpc_prefix_stable(rpc):
    a = rpc.call("prefix", {"role": "main"})["result"]["text"]
    b = rpc.call("prefix", {"role": "main"})["result"]["text"]
    assert a == b and "Memory (Intuition)" in a


def test_rpc_capture_turn_and_note(rpc):
    out = rpc.call("capture_turn", {"text": "hello memory", "role": "user"})
    assert out["result"]["line"] == 1
    out = rpc.call("note", {"text": "june likes agendas early",
                            "evidence": "june likes agendas early"})
    assert out["result"]["ok"]
    # a fresh note is searchable as pending through a second client view
    out = rpc.call("search", {"queries": ["june agendas"]})
    assert any(r.get("pending") for r in out["result"]["records"])


def test_rpc_bad_method_is_an_error_not_a_crash(rpc):
    out = rpc.call("no_such_method")
    assert "error" in out
    assert rpc.call("ping")["result"]["pong"]


# -- triggers ------------------------------------------------------------------------

def test_idle_trigger(store, index):
    from intuition.steward.tick import should_light, tick
    do, why = should_light(store)
    assert not do
    inbox.append(store, kind="fact", text="x", source="user", evidence="x")
    do, why = should_light(store)
    assert not do, "one small note alone should not fire the 10-item trigger"
    result = tick(store, index)
    assert result["pass"] in ("none", "deep"), "small note alone must not force a light pass"


def test_explicit_note_triggers_after_five_minutes(store, index):
    from intuition.steward.tick import should_light
    inbox.append(store, kind="fact", text="remember the kicker deadline",
                 source="user", evidence="remember the kicker deadline",
                 explicit=True)
    # entry is fresh: no trigger yet
    do, _ = should_light(store)
    assert not do
    # age the state file's view: rewrite ts 10 minutes old
    items = store.read_jsonl("inbox/pending.jsonl")
    import time
    old = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 660))
    items[0]["ts"] = old
    import json as j
    store.resolve("inbox/pending.jsonl").write_text(
        "".join(j.dumps(i) + "\n" for i in items))
    do, why = should_light(store)
    assert do and "explicit" in why
