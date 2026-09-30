"""RPC server end-to-end over stdio and triggers."""

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


class HostRpcClient(RpcClient):
    """A client that also answers the child's model requests, as Pi does."""

    def __init__(self, store_path):
        super().__init__(store_path)
        self.model_requests: list[dict] = []

    def serve(self, method, params, reply_text="", reply_error=None):
        """Send a request, answer any model request it makes, return its response."""
        self.nid += 1
        self.p.stdin.write(json.dumps({"id": self.nid, "method": method,
                                       "params": params}) + "\n")
        self.p.stdin.flush()
        while True:
            line = self.p.stdout.readline()
            assert line, self.p.stderr.read()
            message = json.loads(line)
            if "llm" in message:
                self.model_requests.append(message["llm"])
                answer = {"id": message["llm"]["id"], "method": "llm_result"}
                if reply_error is not None:
                    answer["error"] = reply_error
                else:
                    answer["result"] = {"text": reply_text}
                self.p.stdin.write(json.dumps(answer) + "\n")
                self.p.stdin.flush()
                continue
            return message


@pytest.fixture
def host_rpc(store):
    client = HostRpcClient(store.root)
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


def test_rpc_config_round_trip_over_stdio(rpc, store):
    """The Pi command's whole path: shown, written, and readable again."""
    shown = rpc.call("config_show")["result"]["settings"]
    keys = {row["key"]: row for row in shown}
    assert keys["steward.idle_minutes"]["value"] == 30
    assert keys["steward.idle_minutes"]["overridden"] is False

    written = rpc.call("config_set",
                       {"key": "steward.idle_minutes", "value": "11"})["result"]
    assert written == {"key": "steward.idle_minutes", "value": 11}
    assert "idle_minutes = 11" in store.read_text("intuition.toml")

    again = {row["key"]: row for row in rpc.call("config_show")["result"]["settings"]}
    assert again["steward.idle_minutes"]["value"] == 11
    assert again["steward.idle_minutes"]["overridden"] is True

    bad = rpc.call("config_set", {"key": "steward.not_a_key", "value": "1"})
    assert "error" in bad and "unknown setting" in bad["error"]
    assert rpc.call("ping")["result"]["pong"] is True


# -- the host's own model on the wire -------------------------------------------------

def _pending_note(store):
    return inbox.append(store, kind="fact", text="june wants the agenda early",
                        source="user", evidence="june wants the agenda early")


def test_rpc_tick_plans_on_the_host_session_model(host_rpc, store):
    """The whole zero-config path: no model settings, the Pi session's model plans."""
    _pending_note(store)
    out = host_rpc.serve(
        "tick", {"reason": "session_end", "session_end": True,
                 "host_model": {"available": True, "label": "test/model"}},
        reply_text='{"ops": [{"op": "noop"}], "summary": "s"}')

    assert "error" not in out, out
    assert out["result"]["tick"]["pass"] == "light"
    assert out["result"]["tick"]["model"] == "host"
    assert host_rpc.model_requests, "the child must ask the host for the plan"
    assert host_rpc.model_requests[0]["system"]
    assert "batch" in host_rpc.model_requests[0]["user"]


def test_rpc_tick_without_a_host_model_stays_deterministic(host_rpc, store):
    _pending_note(store)
    out = host_rpc.serve(
        "tick", {"reason": "session_end", "session_end": True,
                 "host_model": {"available": False}})

    assert out["result"]["tick"]["model"] == "deterministic"
    assert host_rpc.model_requests == []


def test_rpc_tick_keeps_the_batch_when_the_host_model_fails(host_rpc, store):
    item = _pending_note(store)
    out = host_rpc.serve(
        "tick", {"reason": "session_end", "session_end": True,
                 "host_model": {"available": True, "label": "test/model"}},
        reply_error="the active model refused")

    assert "the active model refused" in out["result"]["tick"]["error"]
    pending = {row["id"] for row in store.read_jsonl("inbox/pending.jsonl")}
    assert item["id"] in pending, "an unplanned proposal waits for the next pass"
    assert host_rpc.call("ping")["result"]["pong"], "the channel still works"


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
