"""Agent tools — one implementation, three frontends (CLI, RPC, Hermes).

Subagents get only memory_search / memory_read. The main assistant gets the
rest. secure_get is main-only and opt-in.
"""

from __future__ import annotations

import time

from . import context, inbox
from . import search as search_mod
from .model import NOW_MAX_CHARS


def memory_search(store, index, args: dict) -> dict:
    queries = args.get("queries") or ([args["query"]] if args.get("query") else [])
    if not queries:
        return {"error": "queries required"}
    hits = search_mod.search(
        store, index, queries,
        rtype=args.get("type"), as_of=args.get("as_of"),
        limit=int(args.get("limit", 8)))
    if args.get("budget", True):
        hits = search_mod.budget(store, hits)
    return {"records": [h.to_dict() for h in hits],
            "rendered": search_mod.render_hits(hits)}


def memory_read(store, index, args: dict) -> dict:
    rid = args.get("id_or_name") or args.get("id") or ""
    if not rid:
        return {"error": "id_or_name required"}
    rec = store.load_record(rid)
    if rec is None:
        search_mod.read_after_miss(index, rid, "")
        return {"error": f"no record for {rid!r}"}
    search_mod.read_after_miss(index, rid, rec.id)
    index.record_usage(rec.id)
    as_of = args.get("as_of")
    facts = rec.fact_lines(as_of=as_of) if as_of else rec.fact_lines(store.today())
    linked = []
    for link in rec.links:
        other = store.load_record(link.target)
        linked.append(f"{link.rel} → {link.target}"
                      + (f" ({other.name})" if other else ""))
    return {"id": rec.id, "type": rec.type, "name": rec.name,
            "aliases": rec.aliases, "facts": facts, "links": linked,
            "front": {k: v for k, v in rec.front.items()
                      if k not in ("id", "type", "name")}}

def memory_timeline(store, index, args: dict) -> dict:
    period = args.get("period", "daily")
    date = args.get("date") or store.today()
    if period == "weekly":
        rel = f"timeline/weekly/{_weekfile(date)}"
    else:
        rel = f"timeline/daily/{date}.md"
    text = store.read_text(rel)
    return {"path": rel, "content": text} if text else {"path": rel, "content": "", "error": "no timeline entry"}


def memory_note(store, index, args: dict) -> dict:
    entry = inbox.append(
        store, kind=args.get("kind", "fact"), text=args.get("text", ""),
        source=args.get("source", "user"), evidence=args.get("evidence", ""),
        about=args.get("about"), confidence=float(args.get("confidence", 0.9)),
        host=args.get("host", "hermes"), session=args.get("session", ""),
        agent=args.get("agent", "main"),
        explicit=bool(args.get("explicit")) or "remember" in args.get("text", "").lower())
    return {"ok": True, "inbox_id": entry["id"],
            "note": "pending — the Steward will confirm it"}


def memory_forget(store, index, args: dict) -> dict:
    entry = inbox.append(
        store, kind="forget", text=args.get("text", ""),
        source="user", evidence=args.get("evidence", "forget request"),
        about=args.get("about"))
    return {"ok": True, "inbox_id": entry["id"],
            "note": "forget requested — Steward will remove it in the next light pass"}


def memory_now(store, index, args: dict) -> dict:
    content = args.get("content", "").strip()
    if not content:
        return {"error": "content required"}
    header = f"# NOW — updated {store.now_iso()}\n\n"
    text = (header + content)[:NOW_MAX_CHARS]
    store.write("working/NOW.md", text)
    return {"ok": True, "chars": len(text),
            "note": "NOW.md is your working state; the Steward reads it but never rewrites it"}


def memory_brief(store, index, args: dict) -> dict:
    agent = args.get("agent", "")
    if not agent:
        return {"error": "agent required"}
    task_id = args.get("task_id") or context.new_task_id(store)
    text = context.build_brief(
        store, index, agent=agent, goal=args.get("goal", ""),
        output=args.get("output", ""), tools=args.get("tools", ""),
        boundaries=args.get("boundaries", ""), task_id=task_id,
        decision_ids=args.get("decision_ids"))
    store.write(f"tasks/{task_id}/brief.md", text)
    prefix = context.build_subagent_prefix(store, agent)
    return {"task_id": task_id, "brief": text, "memory_prefix": prefix,
            "output_schema": context.LEARNINGS_SCHEMA}


def memory_learn(store, index, args: dict) -> dict:
    task_id = args.get("task_id", "")
    learnings = args.get("learnings") or []
    if not task_id:
        return {"error": "task_id required"}
    accepted = []
    for lrn in learnings:
        lrn = dict(lrn)
        lrn.setdefault("source", "agent")
        lrn.setdefault("confidence", 0.8)
        entry = inbox.append(
            store, kind=lrn["kind"], text=lrn["text"], source=lrn["source"],
            evidence=lrn.get("evidence", ""), about=lrn.get("about"),
            confidence=lrn["confidence"], agent=args.get("agent", "subagent"),
            task_id=task_id)
        accepted.append(entry["id"])
    store.append_jsonl(
        f"tasks/{task_id}/learnings.jsonl",
        {"ts": store.now_iso(), "learnings": learnings})
    return {"ok": True, "accepted": accepted,
            "policy": "procedure auto-accepted; preference/decision need main review; "
                      "external quarantined if imperative"}


def memory_secure_get(store, index, args: dict) -> dict:
    if not store.section("safety", "secure_enabled"):
        return {"error": "secure disabled in intuition.toml [safety] secure_enabled"}
    key = args.get("key", "")
    if not key or "/" in key or ".." in key:
        return {"error": "bad key"}
    p = store.resolve(f"secure/{key}")
    if not p.exists() and not key.endswith(".md"):
        p = store.resolve(f"secure/{key}.md")
    if not p.exists():
        return {"error": f"no secure item {key!r}"}
    return {"key": key, "value": p.read_text().strip()}


def memory_status(store, index, args: dict) -> dict:
    from .steward.state import LAST_MODEL, State
    batch = inbox.read_batch(store)
    zero, total = index.miss_rate()
    return {"inbox": len(batch), "records": len(store.scan_records()),
            "misses": f"{zero}/{total}",
            "quarantine": len(inbox.read_quarantine(store)),
            "last_pass_model": State(store).data.get(LAST_MODEL, "none yet"),
            "head": store.head()}


TOOL_HANDLERS = {
    "memory_search": memory_search,
    "memory_read": memory_read,
    "memory_timeline": memory_timeline,
    "memory_note": memory_note,
    "memory_forget": memory_forget,
    "memory_now": memory_now,
    "memory_brief": memory_brief,
    "memory_learn": memory_learn,
    "memory_secure_get": memory_secure_get,
    "memory_status": memory_status,
}

SUBAGENT_TOOLS = {"memory_search", "memory_read"}


def _s(**props) -> dict:
    return {"type": "string", **props}


# The one source of truth for the tool surface. The Hermes adapter reads it
# directly, and `intuition install pi` writes it beside the Pi extension, so the
# two hosts cannot drift apart.
MEMORY_TOOL_SCHEMAS: dict[str, dict] = {
    "memory_search": {
        "description": "Search memory. Give 1-3 phrasings of your query in the user's own words.",
        "parameters": {
            "type": "object",
            "properties": {
                "queries": {"type": "array", "items": {"type": "string"},
                            "minItems": 1, "maxItems": 3},
                "as_of": _s(description="YYYY-MM or YYYY-MM-DD, what was true then"),
                "type": _s(description="person|org|preference|topic|decision|workstream|procedure"),
            },
            "required": ["queries"],
        },
    },
    "memory_read": {
        "description": "Read one full memory record by id or name.",
        "parameters": {"type": "object", "properties": {"id_or_name": _s()},
                       "required": ["id_or_name"]},
    },
    "memory_timeline": {
        "description": "Read a daily or weekly timeline rollup.",
        "parameters": {"type": "object", "properties": {
            "period": _s(description="daily|weekly"), "date": _s()}},
    },
    "memory_note": {
        "description": "Save a memory proposal. evidence MUST be the user's exact words.",
        "parameters": {"type": "object", "properties": {
            "text": _s(),
            "kind": _s(description="fact|preference|decision|procedure|question"),
            "about": _s(description="record id this is about, if known"),
            "evidence": _s(), "confidence": {"type": "number"}},
            "required": ["text", "evidence"]},
    },
    "memory_forget": {
        "description": "Request removal of a memory.",
        "parameters": {"type": "object", "properties": {
            "about": _s(description="record id"), "text": _s(description="text to remove")},
            "required": ["about", "text"]},
    },
    "memory_now": {
        "description": "Replace working/NOW.md: your plan, open threads, task ids.",
        "parameters": {"type": "object", "properties": {"content": _s()},
                       "required": ["content"]},
    },
    "memory_brief": {
        "description": "Build a delegation brief with decisions and relevant memory. "
                       "Returns brief, memory_prefix and output_schema for the child.",
        "parameters": {"type": "object", "properties": {
            "agent": _s(), "goal": _s(), "output": _s(), "tools": _s(),
            "boundaries": _s()},
            "required": ["agent", "goal"]},
    },
    "memory_learn": {
        "description": "Record a returning subagent's learnings (review them first).",
        "parameters": {"type": "object", "properties": {
            "task_id": _s(),
            "learnings": {"type": "array", "items": {
                "type": "object",
                "required": ["kind", "text", "source", "evidence", "confidence"],
                "properties": {
                    "kind": {"type": "string",
                             "enum": ["fact", "preference", "decision", "procedure", "question"]},
                    "about": {"type": "string"}, "text": {"type": "string"},
                    "source": {"type": "string", "enum": ["user", "agent", "external"]},
                    "evidence": {"type": "string"},
                    "confidence": {"type": "number"}}}}},
            "required": ["task_id", "learnings"]},
    },
    "memory_secure_get": {
        "description": "Read one item from the secure scope (main assistant only, opt-in).",
        "parameters": {"type": "object", "properties": {"key": _s()},
                       "required": ["key"]},
    },
    "memory_status": {
        "description": "Inbox, records, miss rate, quarantine and the current commit.",
        "parameters": {"type": "object", "properties": {}},
    },
}

TOOL_ORDER = ("memory_search", "memory_read", "memory_timeline", "memory_note",
              "memory_forget", "memory_now", "memory_brief", "memory_learn",
              "memory_secure_get", "memory_status")


def schemas_for(role: str) -> list[dict]:
    """Tool definitions a role may call, in a stable order."""
    if role == "cron":
        return []
    allowed = SUBAGENT_TOOLS if role == "subagent" else set(TOOL_ORDER)
    return [{"name": name, **MEMORY_TOOL_SCHEMAS[name]}
            for name in TOOL_ORDER if name in allowed]


def host_manifest() -> dict:
    """The tool manifest the Pi package ships and `intuition install pi` writes.

    One definition, so the copy committed beside the extension, the copy the
    installer generates, and the schemas the Hermes adapter reads cannot drift.
    """
    return {"main": schemas_for("main"), "subagent": schemas_for("subagent")}


def handle_tool_call(store, index, tool_name: str, args: dict,
                     *, role: str = "main") -> dict:
    if tool_name not in TOOL_HANDLERS:
        return {"error": f"unknown tool {tool_name}"}
    if role == "subagent" and tool_name not in SUBAGENT_TOOLS:
        return {"error": f"{tool_name} is main-assistant only"}
    return TOOL_HANDLERS[tool_name](store, index, args)


def _weekfile(date: str) -> str:
    try:
        t = time.strptime(date, "%Y-%m-%d")
    except ValueError:
        return date
    return f"{t.tm_year}-W{time.strftime('%W', t)}"
