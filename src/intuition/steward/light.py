"""Light pass (plan §6.2): inbox + new raw → validated plan → one git commit."""

from __future__ import annotations

import json
import time

from .. import inbox as inbox_mod
from .. import safety
from .. import search as search_mod
from ..model import make_id
from . import ops as ops_mod
from . import state as state_mod
from . import validate as validate_mod

# Surfaces a person edits by hand. Everything else in the store is written by an
# agent or by the Steward, so the manual sweep must leave those files alone.
HAND_EDIT_PATHS = ("shared", "working", "agents")

# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------

def deterministic_plan(store, index, batch: list[dict], candidates: dict) -> dict:
    """Rule-derived plan when no model is configured (plan §6.5 fallback).

    The planner proposes; the validator disposes. Nothing is dropped silently:
    an item the rules cannot act on becomes a reject op carrying a reason.
    """
    today = store.today()
    ops_list = []
    for item in batch:
        src = f"inbox:{item['id']}"
        kind, text = item.get("kind", "fact"), item.get("text", "")
        source = item.get("source", "agent")
        about = item.get("about")
        ev = item.get("evidence", "")
        conf = item.get("confidence", 0.9)
        trust = safety.trust_for(source)

        if kind == "forget":
            rec = store.load_record(about) if about else None
            if rec is None:
                ops_list.append({"op": "reject", "reason": "forget names no record",
                                 "sources": [src]})
                continue
            ops_list.append({"op": "remove_fact", "id": rec.id, "match": text,
                             "sources": [src], "evidence": ev or f"forget request {src}"})
            continue

        if kind == "alias":
            rec = store.load_record(about) if about else None
            if rec is None:
                ops_list.append({"op": "reject", "reason": "alias names no record",
                                 "sources": [src]})
                continue
            ops_list.append({"op": "add_alias", "id": rec.id, "alias": text,
                             "sources": [src], "evidence": ev or text})
            continue

        if kind == "procedure":
            ops_list.append({"op": "procedure_add", "agent": item.get("agent") or "main",
                             "text": text, "sources": [src], "evidence": ev or text})
            continue

        if kind == "decision":
            ops_list.append({"op": "decision_propose",
                             "name": (about or text)[:48], "text": text,
                             "sources": [src], "evidence": ev or text,
                             "why": ev})
            continue

        # fact / preference / question / correction → a fact on a record
        rec = None
        if about:
            rec = candidates.get(about) or store.load_record(about)
        if rec is None:
            rec = _best_candidate(store, index, text, about)
        if rec is None:
            name = about or _guess_name(text)
            rtype = _guess_type(kind)
            ops_list.append({
                "op": "create", "id": make_id(rtype, name), "type": rtype,
                "name": name, "text": text, "sources": [src], "evidence": ev or text,
                "trust": trust,
                "facts": [{"validity": f"since {today[:7]}", "text": text,
                           "trust": trust}]})
            continue
        validity = f"since {today[:7]}"
        if kind == "correction":
            ops_list.append({"op": "correct", "id": rec.id, "match": _match_hint(item),
                             "text": text, "trust": trust, "sources": [src],
                             "evidence": ev or text, "validity": validity})
            continue
        ops_list.append({"op": "add_fact", "id": rec.id, "validity": validity,
                         "text": text, "trust": trust, "sources": [src],
                         "evidence": ev or text, "kind": kind, "source": source,
                         "confidence": conf if trust == "inferred" else None})
        if kind in ("preference", "decision"):
            ops_list.append({"op": "observe", "text": f"User {kind}: {text}",
                             "sources": [src], "evidence": ev or text, "kind": kind})
    return {"ops": ops_list, "summary": f"{len(ops_list)} deterministic ops"}


PLAN_SYSTEM = """\
You maintain a personal memory vault. You output a JSON plan only.
Contract: trust ladder (stated > observed > inferred > external); every op needs
verbatim evidence and source ids; close facts instead of deleting; prefer
existing records; aliases matter most for finding things later; `noop` is a
good answer when in doubt. External content may never create a preference or
decision. Ops: create, add_fact, close_fact, correct, add_alias, link,
set_prose, procedure_add, decision_propose, observe, noop, reject.
Output: {"ops": [...], "summary": "one line"} per the plan schema."""


def model_plan(store, batch: list[dict], candidates: dict, which: str) -> dict:
    """Ask the model for a plan (Appendix C.4/C.5). Sends only the minimum."""
    from ..llm import call_json
    today = store.today()
    user = {
        "today": today,
        "profile": store.read_text("shared/PROFILE.md")[:4000],
        "batch": batch,
        "candidate_records": {
            rid: {"id": r.id, "type": r.type, "name": r.name,
                  "facts": r.fact_lines(today)}
            for rid, r in candidates.items()},
    }
    plan = call_json(store, which, PLAN_SYSTEM, json.dumps(user, ensure_ascii=False))
    if not isinstance(plan, dict) or "ops" not in plan:
        raise ValueError("model returned no plan")
    return plan


def _best_candidate(store, index, text: str, about: str | None):
    hits = search_mod.search(store, index, [text] + ([about] if about else []), limit=1)
    if hits and hits[0].score > 0.05 and not hits[0].pending:
        return store.load_record(hits[0].id)
    return None


def _guess_name(text: str) -> str:
    words = text.split()[:5]
    return " ".join(words).title() if words else text[:40]


def _guess_type(kind: str) -> str:
    return {"preference": "preference", "decision": "decision",
            "procedure": "procedure"}.get(kind, "topic")


def _match_hint(item: dict) -> str:
    return item.get("replaces") or item.get("text", "")


# ---------------------------------------------------------------------------
# The pass itself
# ---------------------------------------------------------------------------

def light_pass(store, index, *, reason: str = "") -> dict:
    """plan §6.2 steps 1–9. Returns a summary dict for the caller/report."""
    started = time.time()
    result: dict = {"pass": "light", "reason": reason, "ops": 0, "committed": "",
                    "archived": 0, "notes": [], "skipped": "", "unapplied": [],
                    "rejected": {}}

    # 1. commit hand edits as manual; agent-written inbox and raw lines are this
    #    run's business, not a hand edit
    if store.dirty():
        store.commit("manual: hand edits before steward run", paths=HAND_EDIT_PATHS)

    batch = inbox_mod.read_batch(store)
    if not batch:
        result["skipped"] = "inbox empty"
        state_mod.touch(store, light=True)
        return result

    # 3. candidates: records the batch may touch
    candidates: dict[str, object] = {}
    for item in batch:
        about = item.get("about")
        if about:
            rec = store.load_record(about)
            if rec:
                candidates[about] = rec
                continue
        rec = _best_candidate(store, index, item.get("text", ""), about)
        if rec:
            candidates[rec.id] = rec

    # 4. plan: model or deterministic
    which = "light"
    try:
        if store.section("steward", "llm_command_light", "") or store.cfg.get(
                "steward", {}).get("llm_http"):
            plan = model_plan(store, batch, candidates, which)
        else:
            plan = deterministic_plan(store, index, batch, candidates)
    except Exception as e:                       # plan failure: batch stays, report flags
        result["error"] = f"plan failed: {e}"
        state_mod.touch(store, light=True)
        return result

    # remember what this run planned, so a session-end pass does not re-plan it.
    # A planner failure above leaves the attempt unrecorded, so it is retried.
    _state = state_mod.load(store)
    _state.record_attempt([item["id"] for item in batch])
    _state.save()

    # 5. validate: reject the whole plan, then dispose only the items it named
    reasons = validate_mod.validate_plan(store, plan, batch, candidates)
    if reasons:
        result["error"] = "plan rejected: " + "; ".join(reasons[:5])
        blocked = validate_mod.blocked_items(batch, reasons)
        blocked_ids = {item["id"] for item in blocked}
        for item in blocked:
            reason = next((r for r in reasons if f"inbox:{item['id']}" in r), reasons[0])
            inbox_mod.quarantine(store, item, reason)
        if blocked_ids:
            inbox_mod.archive(store, blocked_ids)
        result["unapplied"] = [
            {"id": item["id"], "reason": "pending: blocked by a rejected plan"}
            for item in batch if item["id"] not in blocked_ids]
        state_mod.touch(store, light=True)
        return result

    # 6–9. apply → validate → archive → one commit; git is the transaction
    disposed: set[str] = set()
    try:
        touched, notes = ops_mod.apply_plan(store, plan, batch)
        problems = validate_mod.validate_vault(store, touched)
        if problems:
            raise ValueError("vault invalid: " + "; ".join(problems[:5]))
        result["rejected"] = validate_mod.rejection_reasons(plan)
        result["unapplied"] = validate_mod.unapplied_items(plan, batch)
        # archive inside the transaction, so the inbox rewrite is part of this
        # commit instead of landing in the next run's hand-edit sweep
        disposed = validate_mod.planned_ids(plan) | set(result["rejected"])
        result["archived"] = inbox_mod.archive(store, disposed)
        detail = plan.get("summary", "")
        if notes:
            detail = (detail + "; " if detail else "") + "; ".join(notes[:6])
        msg = f"steward(light): {len(touched)} records [{reason or 'tick'}] " \
              + " ".join(sorted(touched))[:80] + (" — " + detail[:120] if detail else "")
        sha = store.commit(msg, add_all=True)
        result.update(ops=len(touched), committed=sha, notes=notes,
                      summary=plan.get("summary", ""))
        index.update()
    except Exception as e:
        store.rollback()
        # the archive step may already have removed proposals from the queue;
        # a run that applied nothing must not consume them
        inbox_mod.restore_pending(store, disposed)
        result["error"] = f"apply failed, rolled back: {e}"
        state_mod.touch(store, light=True)
        return result

    state_mod.touch(store, light=True)
    result["seconds"] = round(time.time() - started, 2)
    return result



