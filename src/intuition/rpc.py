"""stdio JSON-lines RPC server for the Pi extension (plan §8.2).

Protocol: one JSON object per line.
  → {"id": 1, "method": "search", "params": {...}}
  ← {"id": 1, "result": {...}}          or  {"id": 1, "error": "..."}

Methods mirror the agent tools plus: prefix (stable prompt block), capture_turn,
checkpoint, tick, ping. Run with: `intuition rpc` (implemented in cli? no —
`python -m intuition.rpc`). One process per Pi session; no network port, no MCP.
"""

from __future__ import annotations

import json
import sys

from . import config, context
from .index import Index
from .steward import tick as steward_tick
from .store import Store
from .tools import handle_tool_call


def _prefix(store, index, params):
    role = params.get("role", "main")
    if role == "subagent":
        return {"text": context.build_subagent_prefix(
            store, params.get("agent", "child"))}
    if role == "cron":
        return {"text": context.MAIN_CONTRACT}
    return {"text": context.build_main_prefix(store, index)}


def _capture_turn(store, index, params):
    """Main only: append the turn to raw/<day>.jsonl (plan §8.2)."""
    line_no = 0
    p = store.dir(f"raw/{store.today()}.jsonl")
    if p.exists():
        line_no = sum(1 for _ in p.open())
    entry = {
        "ts": store.now_iso(),
        "host": params.get("host", "pi"),
        "session": params.get("session", ""),
        "role": params.get("role", "user"),
        "text": str(params.get("text", ""))[:4000],
    }
    store.append_jsonl(f"raw/{store.today()}.jsonl", entry)
    return {"line": line_no + 1}          # ^raw:<day>#<line> pointers use this


def _checkpoint(store, index, params):
    """On compaction: write NOW + observation draft; idempotent by digest."""
    import hashlib
    text = str(params.get("summary_text", ""))[:4000]
    digest = hashlib.sha256(text.encode()).hexdigest()[:12]
    path = f"observations/drafts/{store.today()}-{digest}.md"
    if not store.dir("observations/drafts").exists() or \
            not store.resolve(path).exists():
        store.write(path, f"## compaction checkpoint {store.now_iso()}\n{text}\n")
    return {"checkpoint": f"intuition:{path}"}


def dispatch(store: Store, index: Index, method: str, params: dict):
    if method == "ping":
        return {"pong": True, "store": str(store.root)}
    if method == "prefix":
        return _prefix(store, index, params)
    if method == "capture_turn":
        return _capture_turn(store, index, params)
    if method == "checkpoint":
        return _checkpoint(store, index, params)
    if method == "tick":
        result = steward_tick(store, index,
                              light=params.get("light", False),
                              deep=params.get("deep", False),
                              session_end=params.get("session_end", False),
                              reason=params.get("reason", "session_end"))
        return {"tick": result}
    if method == "config_show":
        return {"settings": config.describe(store)}
    if method == "config_set":
        value = config.set_value(store, params["key"], params["value"])
        return {"key": params["key"], "value": value}
    if method == "config_help":
        return {"text": config.settings_help()}
    if method == "timeline":
        return handle_tool_call(store, index, "memory_timeline", params)
    if method == "note":
        return handle_tool_call(store, index, "memory_note", params)
    if method == "forget":
        return handle_tool_call(store, index, "memory_forget", params)
    if method == "now":
        return handle_tool_call(store, index, "memory_now", params)
    if method == "brief":
        return handle_tool_call(store, index, "memory_brief", params)
    if method == "learn":
        return handle_tool_call(store, index, "memory_learn", params)
    if method == "secure_get":
        return handle_tool_call(store, index, "memory_secure_get", params)
    if method == "status":
        return handle_tool_call(store, index, "memory_status", params)
    if method in ("search", "read"):
        return handle_tool_call(store, index, f"memory_{method}", params)
    raise KeyError(f"unknown method {method!r}")


def main() -> None:
    store = Store()
    index = Index(store)
    index.update()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            rid = req.get("id")
            result = dispatch(store, index, req.get("method", ""),
                              req.get("params", {}) or {})
            _emit({"id": rid, "result": result})
        except Exception as e:                    # never crash the stream
            _emit({"id": req.get("id") if isinstance(req, dict) else None,
                   "error": str(e)})
    index.close()


def _emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, default=str) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
