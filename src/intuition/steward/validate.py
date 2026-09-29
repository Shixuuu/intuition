"""Deterministic plan validation (plan §6.4) — no AI in this file.

Each rule rejects a whole plan (fail closed); the light pass keeps the batch
in the inbox so nothing is lost when a plan is rejected.
"""

from __future__ import annotations

import re

from ..model import BRIEF_MAX_CHARS, MAX_RECORD_BODY, NOW_MAX_CHARS
from . import ops as ops_mod

_URL_RE = re.compile(r"^url:")


def validate_plan(store, plan: dict, batch: list[dict], candidates: dict) -> list[str]:
    """Run every §6.4 rule. Returns a list of reject reasons (empty = valid)."""
    reasons: list[str] = []
    max_ops = int(store.section("steward", "max_plan_ops", 60))

    schema_err = ops_mod.validate_schema(plan, max_ops)
    if schema_err:
        return [schema_err]

    # evidence index: what the cited sources actually said
    evidence_pool: dict[str, list[str]] = {}
    for item in batch:
        src = f"inbox:{item['id']}"
        evidence_pool[src] = [item.get("evidence", ""), item.get("text", "")]
    for rid, rec in candidates.items():
        evidence_pool[f"rec:{rid}"] = [
            ln.strip("- ") for ln in rec.fact_lines(store.today())] + [rec.name]
    for f in store.dir("raw").glob("*.jsonl") if store.dir("raw").exists() else ():
        for i, line in enumerate(f.read_text().splitlines(), 1):
            evidence_pool[f"raw:{f.stem}#{i}"] = [line]

    recs = store.scan_records()
    today = store.today()

    for op in plan.get("ops", []):
        name = op.get("op", "")
        tag = f"{name}:{op.get('id', op.get('name', ''))}"

        # Evidence rule: verbatim quote must appear in the cited source (URLs exempt)
        ev = str(op.get("evidence", ""))
        sources = op.get("sources", [])
        if name not in ("noop", "reject") and sources:
            if not any(_URL_RE.match(str(s)) for s in sources):
                pool = []
                for s in sources:
                    pool.extend(evidence_pool.get(str(s), []))
                needle = ev.strip().casefold()
                if needle and needle not in ("", "url"):
                    hay = " ".join(pool).casefold()
                    if needle not in hay:
                        reasons.append(f"evidence: {tag} quote not found in cited source")

        # Trust / imperative / secure rules
        kind = op.get("kind", "")
        source = op.get("source", "agent")
        pseudo = {"op": name if name in ("create", "add_fact") else "add_fact",
                  "kind": kind, "source": source, "trust": op.get("trust", ""),
                  "id": str(op.get("id", "")), "path": "",
                  "evidence": ev, "text": str(op.get("text", ""))}
        safety_err = _safety(store, pseudo, kind, source)
        if safety_err:
            reasons.append(f"{tag}: {safety_err}")

        # Id rules
        if name == "create" and op.get("id") in recs:
            reasons.append(f"id: {tag} already exists")
        if name in ("add_fact", "close_fact", "correct", "add_alias", "link",
                    "set_prose", "remove_fact") and op.get("id") not in recs:
            reasons.append(f"id: {tag} unknown record")

        # Size rule: record over cap → shorten request for deep pass, op rejected now
        rec = recs.get(str(op.get("id", "")))
        if rec is not None and rec.body_chars() + len(str(op.get("text", ""))) > MAX_RECORD_BODY:
            reasons.append(f"size: {tag} would push {op['id']} over {MAX_RECORD_BODY} chars")

        # No silent delete: only close_fact / correct / remove_fact (forget) / expiry
        if name not in ("close_fact", "correct", "remove_fact") and "delete" in op:
            reasons.append(f"delete: {tag} attempted a silent delete")

    return reasons


def _safety(store, pseudo: dict, kind: str, source: str):
    from ..safety import is_imperative, validate_op_safety
    err = validate_op_safety(pseudo)
    if err:
        return err
    if kind == "external" and store.section(
            "safety", "quarantine_external_imperatives", True) \
            and source == "external" and is_imperative(pseudo["text"] + " " + pseudo["evidence"]):
        return "imperative filter"
    return None


def validate_vault(store, touched: set[str]) -> list[str]:
    """plan §6.2 step 7: parse every touched file, links resolve, size caps."""
    problems: list[str] = []
    recs = store.scan_records()
    for rid in touched:
        rec = recs.get(rid)
        if rec is None:
            problems.append(f"{rid}: file missing after apply")
            continue
        if rec.body_chars() > MAX_RECORD_BODY:
            problems.append(f"{rid}: over {MAX_RECORD_BODY} chars — needs shorten (deep pass)")
        for link in rec.links:
            if link.target not in recs:
                problems.append(f"{rid}: link target {link.target} does not resolve")
    now = store.read_text("working/NOW.md")
    if len(now) > NOW_MAX_CHARS:
        problems.append(f"working/NOW.md over {NOW_MAX_CHARS} chars")
    return problems
