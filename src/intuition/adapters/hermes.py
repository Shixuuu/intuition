"""Hermes Agent MemoryProvider adapter (plan §8.1).

Implements the real Hermes contract (agent/memory_provider.py in the host):
  * class attr pre_compress_checkpoint_api_version = 2 → receive
    on_pre_compress(messages, *, require_checkpoint=False), must be idempotent,
  * all background work through spawn_context_thread (profile contextvars),
  * handle_tool_call returns a JSON string,
  * agent_context ∈ {primary, subagent, cron}.

Hermes `delegate_task` children skip external providers entirely
(skip_memory=True), so per plan §14 Q1 the subagent's memory rides in the
brief text: memory_brief returns brief + memory_prefix + output_schema, and
the main assistant passes them into delegate_task(context=…, output_schema=…).
"""

from __future__ import annotations

import json
import queue
import threading

try:                                     # inside Hermes
    from agent.memory_provider import (MemoryProvider, spawn_context_thread)
    _HERMES = True
except ImportError:                      # standalone / tests
    _HERMES = False

    class MemoryProvider:                # minimal stand-in with the same hooks
        pre_compress_checkpoint_api_version = 1
        def is_available(self): return True
        def initialize(self, session_id, **kw): pass
        def get_tool_schemas(self): return []
        def handle_tool_call(self, tool_name, args, **kw): raise NotImplementedError

    def spawn_context_thread(target, *, name, daemon=True, args=(), kwargs=None):
        t = threading.Thread(target=target, name=name, daemon=daemon,
                             args=args, kwargs=kwargs or {})
        t.start()
        return t

from .. import context as ctx_mod
from ..index import Index
from ..store import Store
from ..tools import SUBAGENT_TOOLS, TOOL_HANDLERS, handle_tool_call


def _schema(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties,
                           "required": required}}


def _s(**props):
    return {"type": "string", **props}


MAIN_TOOL_SCHEMAS = [
    _schema("memory_search",
            "Search memory. Give 1-3 phrasings of your query in the user's own words.",
            {"queries": {"type": "array", "items": {"type": "string"},
                         "minItems": 1, "maxItems": 3},
             "as_of": _s(description="YYYY-MM or YYYY-MM-DD — what was true then"),
             "type": _s(description="person|org|preference|topic|decision|workstream|procedure")},
            ["queries"]),
    _schema("memory_read", "Read one full record by id or name.",
            {"id_or_name": _s()}, ["id_or_name"]),
    _schema("memory_timeline", "Read a daily or weekly timeline rollup.",
            {"period": _s(description="daily|weekly"), "date": _s()}, []),
    _schema("memory_note",
            "Save a memory proposal. evidence MUST be the user's exact words.",
            {"text": _s(), "kind": _s(description="fact|preference|decision|procedure|question"),
             "about": _s(description="record id this is about, if known"),
             "evidence": _s(), "confidence": {"type": "number"}}, ["text", "evidence"]),
    _schema("memory_forget", "Request removal of a memory.",
            {"about": _s(description="record id"), "text": _s(description="text to remove")},
            ["about", "text"]),
    _schema("memory_now", "Replace working/NOW.md — your plan, open threads, task ids.",
            {"content": _s()}, ["content"]),
    _schema("memory_brief",
            "Build a delegation brief with decisions + relevant memory. Returns "
            "brief, memory_prefix and output_schema to pass to delegate_task.",
            {"agent": _s(), "goal": _s(), "output": _s(), "tools": _s(),
             "boundaries": _s()}, ["agent", "goal"]),
    _schema("memory_learn",
            "Record a returning subagent's learnings (review them first).",
            {"task_id": _s(), "learnings": {"type": "array", "items": {
                "type": "object",
                "required": ["kind", "text", "source", "evidence", "confidence"],
                "properties": {
                    "kind": {"enum": ["fact", "preference", "decision", "procedure", "question"]},
                    "about": {"type": "string"}, "text": {"type": "string"},
                    "source": {"enum": ["user", "agent", "external"]},
                    "evidence": {"type": "string"},
                    "confidence": {"type": "number"}}}}},
            ["task_id", "learnings"]),
]


class IntuitionProvider(MemoryProvider):
    pre_compress_checkpoint_api_version = 2

    @property
    def name(self) -> str:
        return "intuition"

    def __init__(self):
        self._store: Store | None = None
        self._index: Index | None = None
        self._role = "primary"
        self._agent = "main"
        self._session = ""
        self._prefix = ""
        self._turns: "queue.Queue[tuple]" = queue.Queue()

    # -- lifecycle -----------------------------------------------------------

    def is_available(self) -> bool:
        try:
            Store()
            return True
        except Exception:
            return False

    def initialize(self, session_id: str, **kwargs) -> None:
        self._store = Store()
        self._index = Index(self._store)
        self._index.update()
        self._session = session_id
        self._role = kwargs.get("agent_context", "primary")
        self._agent = kwargs.get("agent_identity") or kwargs.get("agent") or "main"
        # stable prefix built once per session (plan §5.1; caching principle 5)
        if self._role == "subagent":
            self._prefix = ctx_mod.build_subagent_prefix(self._store, self._agent)
        elif self._role == "cron":
            self._prefix = ctx_mod.MAIN_CONTRACT
        else:
            self._prefix = ctx_mod.build_main_prefix(self._store, self._index)

    def unavailable_reason(self) -> str:
        try:
            Store()
        except Exception as e:
            return str(e)
        return ""

    def identity_signature(self) -> dict:
        return {"role": self._role, "agent": self._agent}

    # -- prompt ----------------------------------------------------------------

    def system_prompt_block(self) -> str:
        return self._prefix

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """≤ 4 records, fast; goes into a <memory-context> fence by the host."""
        from .. import search as search_mod
        hits = search_mod.budget(
            self._store,
            search_mod.search(self._store, self._index, [query], limit=6))
        return search_mod.render_hits(hits)

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        spawn_context_thread(self._warm, name="intuition-warm")

    def _warm(self) -> None:
        try:
            self._index.is_stale() and self._index.update()
        except Exception:
            pass

    # -- capture -----------------------------------------------------------------

    def sync_turn(self, user_content: str, assistant_content: str, *,
                  session_id: str = "", messages=None, turn_author=None) -> None:
        """Primary only; must not block — capture on a context thread."""
        if self._role != "primary":
            return
        self._turns.put((user_content or "", assistant_content or ""))
        spawn_context_thread(self._drain_turns, name="intuition-capture")

    def _drain_turns(self) -> None:
        while True:
            try:
                user, asst = self._turns.get_nowait()
            except queue.Empty:
                return
            try:
                for role, text in (("user", user), ("assistant", asst)):
                    if text.strip():
                        line = sum(1 for _ in
                                   self._store.dir(f"raw/{self._store.today()}.jsonl").open()) \
                            if self._store.dir(f"raw/{self._store.today()}.jsonl").exists() else 0
                        self._store.append_jsonl(
                            f"raw/{self._store.today()}.jsonl",
                            {"ts": self._store.now_iso(), "host": "hermes",
                             "session": self._session, "role": role,
                             "text": str(text)[:4000]})
            except Exception:
                return                       # never break the host turn

    # -- tools ----------------------------------------------------------------------

    def get_tool_schemas(self) -> list[dict]:
        if self._role == "subagent":
            return [s for s in MAIN_TOOL_SCHEMAS
                    if s["name"] in SUBAGENT_TOOLS]
        if self._role == "cron":
            return []
        return MAIN_TOOL_SCHEMAS

    def handle_tool_call(self, tool_name: str, args: dict, **kwargs) -> str:
        role = "subagent" if self._role == "subagent" else "main"
        result = handle_tool_call(self._store, self._index, tool_name,
                                  dict(args or {}), role=role)
        return json.dumps(result, ensure_ascii=False, default=str)

    # -- host hooks --------------------------------------------------------------------

    def on_memory_write(self, action: str, target: str, content: str,
                        metadata=None) -> None:
        """Mirror built-in MEMORY.md/USER.md writes into the inbox (plan §8.1, Q5)."""
        from .. import inbox
        try:
            inbox.append(self._store, kind="fact", text=content[:2000],
                         source="agent", evidence=f"hermes {action} {target}",
                         host="hermes", session=self._session)
        except Exception:
            pass

    def on_pre_compress(self, messages, *, require_checkpoint: bool = False) -> str:
        """Checkpoint API v2: durable + idempotent before returning (plan §8.1)."""
        import hashlib
        texts = []
        for m in (messages or []):
            if isinstance(m, dict) and m.get("role") in ("user", "assistant"):
                c = m.get("content")
                if isinstance(c, str) and c.strip():
                    texts.append(f"{m['role']}: {c[:1500]}")
        digest = hashlib.sha256("\n".join(texts).encode()).hexdigest()[:12]
        path = f"observations/drafts/{self._store.today()}-{digest}.md"
        if not self._store.resolve(path).exists():
            self._store.write(path, f"## compaction checkpoint {self._store.now_iso()}\n"
                              + "\n".join(texts)[:6000] + "\n")
        now = self._store.read_text("working/NOW.md")
        if not now and texts:
            self._store.write("working/NOW.md",
                              f"# NOW — auto checkpoint {self._store.now_iso()}\n\n"
                              + "\n".join(texts[-4:])[:2000] + "\n")
        return f"checkpoint: intuition:{path}"

    def on_delegation(self, task: str, result: str, *, child_session_id: str = "",
                      **kwargs) -> None:
        """Parent-side observation of delegate_task results (plan §8.1)."""
        from .. import inbox
        try:
            inbox.append(self._store, kind="fact", text=result[:1500],
                         source="agent", evidence=f"delegation: {task[:300]}",
                         host="hermes", session=child_session_id or self._session)
        except Exception:
            pass

    def on_session_end(self, messages) -> None:
        spawn_context_thread(self._session_end_tick, name="intuition-end-tick")

    def _session_end_tick(self) -> None:
        try:
            from ..steward import tick as steward_tick
            steward_tick(self._store, self._index, reason="session_end")
        except Exception:
            pass

    def shutdown(self) -> None:
        if self._index:
            try:
                self._index.close()
            except Exception:
                pass
            self._index = None

    # -- config panel ---------------------------------------------------------------------

    def get_config_schema(self) -> list[dict]:
        return [{"name": "store", "type": "string", "default": "~/memory",
                 "description": "Intuition store path"}]

    def save_config(self, values: dict, hermes_home: str) -> None:
        pass

    def backup_paths(self) -> list[str]:
        try:
            return [str(Store().root)]
        except Exception:
            return []


def register(ctx) -> None:
    """Hermes plugin entry point (directory plugin or pip entry point)."""
    try:
        ctx.register_memory_provider(IntuitionProvider())
    except AttributeError:
        pass                      # older host: falls back to subclass discovery
