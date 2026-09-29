"""Agent tools (plan §5.4) — one implementation, three frontends (CLI, RPC, Hermes).

Subagents get only memory_search / memory_read. The main assistant gets the
rest. secure_get is main-only and opt-in (plan §9.3).
"""

from __future__ import annotations

import json
import time

from . import context, inbox, safety, search as search_mod
from .model import BRIEF_MAX_CHARS, NOW_MAX_CHARS


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
        explicit="remember" in args.get("text", "").lower())
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
    existing = store.read_text("working/NOW.md")
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
        # auto-accept policy (plan §7.4)
        if lrn["kind"] == "decision":
            lrn["_status"] = "proposed"          # subagents propose, main decides
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
    if not store.section("safety", "secure_enabled", False):
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
    batch = inbox.read_batch(store)
    zero, total = index.miss_rate()
    return {"inbox": len(batch), "records": len(store.scan_records()),
            "misses": f"{zero}/{total}",
            "quarantine": len(inbox.read_quarantine(store)),
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
