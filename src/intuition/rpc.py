"""stdio JSON-lines RPC server for the Pi extension.

Protocol: one JSON object per line.
  → {"id": 1, "method": "search", "params": {...}}
  ← {"id": 1, "result": {...}}          or  {"id": 1, "error": "..."}

The child also asks the host questions on the same channel, so the Steward can
plan on the session's own model instead of one configured for it:
  → {"llm": {"id": 1, "system": "…", "user": "…"}}
  ← {"id": 1, "method": "llm_result", "result": {"text": "…"}}
A `tick` request carries `host_model: {"available": bool, "label": str}`, which is
what arms that call for the pass it starts.

Methods mirror the agent tools plus: prefix (stable prompt block), capture_turn,
checkpoint, tick, ping. Run with: `intuition rpc` (implemented in cli? no —
`python -m intuition.rpc`). One process per Pi session; no network port, no MCP.
"""

from __future__ import annotations

import json
import queue
import sys
import threading
import time

from . import config, context
from . import llm as llm_mod
from .index import Index
from .llm import LLMError
from .steward import tick as steward_tick
from .store import Store
from .tools import handle_tool_call

# A host model call has to fit inside the tick that started it, and the host
# gives up first (see MODEL_TIMEOUT_MS in the extension), so a stalled model
# comes back as an error the pass can report instead of a bare timeout.
HOST_LLM_TIMEOUT = 110.0

_EOF = object()
_INBOX: queue.Queue[object] = queue.Queue()
_HELD: list[dict] = []                 # requests that arrived during a model call
_llm_seq = 0


def _reader() -> None:
    """Drain stdin in one thread so a model call can wait for its own line."""
    try:
        for line in sys.stdin:
            _INBOX.put(line)
    finally:
        _INBOX.put(_EOF)


def _next_line(timeout: float | None = None):
    """The next line from the host, ``_EOF`` at end of input, None on timeout."""
    try:
        return _INBOX.get(timeout=timeout)
    except queue.Empty:
        return None


def _host_llm(system: str, user: str) -> str:
    """Ask the host session to run one prompt on its active model."""
    global _llm_seq
    _llm_seq += 1
    request_id = _llm_seq
    _emit({"llm": {"id": request_id, "system": system, "user": user}})
    deadline = time.monotonic() + HOST_LLM_TIMEOUT
    while True:
        line = _next_line(max(0.0, deadline - time.monotonic()))
        if line is None:
            raise LLMError(f"host model call timed out after {HOST_LLM_TIMEOUT:.0f}s")
        if line is _EOF:
            raise LLMError("host closed the channel during a model call")
        text = str(line).strip()
        if not text:
            continue
        try:
            message = json.loads(text)
        except json.JSONDecodeError:
            continue
        if message.get("method") == "llm_result" and message.get("id") == request_id:
            if message.get("error"):
                raise LLMError(str(message["error"]))
            return str((message.get("result") or {}).get("text") or "")
        if isinstance(message, dict) and message.get("method"):
            _HELD.append(message)      # a normal request: answer it after this call


def _arm_host_model(params: dict) -> None:
    """Point the Steward at the host session's model, or let go of it."""
    host = params.get("host_model") or {}
    if host.get("available"):
        llm_mod.set_host_transport(
            _host_llm, label=str(host.get("label") or "host session"))
    else:
        llm_mod.clear_host_transport()


def _prefix(store, index, params):
    role = params.get("role", "main")
    if role == "subagent":
        return {"text": context.build_subagent_prefix(
            store, params.get("agent", "child"))}
    if role == "cron":
        return {"text": context.MAIN_CONTRACT}
    return {"text": context.build_main_prefix(store, index)}


def _capture_turn(store, index, params):
    """Main only: append the turn to raw/<day>.jsonl."""
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
        _arm_host_model(params)
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


def _next_message():
    """The next request object, or ``_EOF``. Held requests come first."""
    while True:
        if _HELD:
            return _HELD.pop(0)
        line = _next_line()
        if line is _EOF:
            return _EOF
        text = str(line).strip()
        if not text:
            continue
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            continue


def main() -> None:
    store = Store()
    index = Index(store)
    index.update()
    threading.Thread(target=_reader, name="intuition-rpc-reader",
                     daemon=True).start()
    while True:
        req = _next_message()
        if req is _EOF or not isinstance(req, dict):
            break
        try:
            result = dispatch(store, index, req.get("method", ""),
                              req.get("params", {}) or {})
            _emit({"id": req.get("id"), "result": result})
        except Exception as e:                    # never crash the stream
            _emit({"id": req.get("id"), "error": str(e)})
    index.close()


def _emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, default=str) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
