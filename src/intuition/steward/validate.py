"""Deterministic plan validation — no AI in this file.

Every plan passes through here, so the trust decision lives here rather than in
the planner. An op's own ``source``/``kind`` fields are a claim by whoever wrote
the plan; the cited inbox item is the evidence, and it decides. A plan that
would turn external content into a procedure, a preference, or a decision is
rejected whole (fail closed), and the pass disposes the items a rejection names
while keeping the rest of the batch pending.
"""

from __future__ import annotations

import re

from ..model import MAX_RECORD_BODY, NOW_MAX_CHARS
from ..safety import is_imperative, validate_op_safety
from . import ops as ops_mod

_URL_RE = re.compile(r"^url:")
EXTERNAL = "external"
# Ops that may only be backed by the user or the main assistant.
AUTHORED_OPS = ("procedure_add", "decision_propose", "set_prose", "add_alias",
                "link", "observe", "correct", "remove_fact", "close_fact")
# The only shapes external content may take, both carrying the external trust tag.
EXTERNAL_OPS = ("add_fact", "create")
# Kinds that become durable instruction or profile text.
PROTECTED_KINDS = ("preference", "decision", "procedure")
# Ops carrying text that a future agent reads as an instruction, a profile line,
# a record's prose, or an alias. Their text must come from the item they cite.
CONTENT_OPS = ("procedure_add", "decision_propose", "set_prose", "observe",
               "correct", "add_alias")
APPLY_OPS = tuple(n for n in ops_mod.PLAN_OPS if n not in ("noop", "reject"))


def citation_trust(op: dict, batch_index: dict[str, dict]) -> str:
    """What the op's cited inbox items say about where this came from.

    ``external`` when any cited item is external, ``local`` when at least one is
    user- or agent-authored, ``none`` when the op cites no inbox item at all. The
    cited item decides: an op's own ``source`` field is a claim by whoever wrote
    the plan, including a model.
    """
    cited = [batch_index[str(s)] for s in op.get("sources", []) if str(s) in batch_index]
    if not cited:
        return "none"
    if any(item.get("source") == EXTERNAL for item in cited):
        return EXTERNAL
    return "local"


def external_citations(op: dict, batch_index: dict[str, dict]) -> list[tuple[str, dict]]:
    """``(source token, item)`` for every cited inbox item that came from outside."""
    return [(str(s), batch_index[str(s)]) for s in op.get("sources", [])
            if str(s) in batch_index and batch_index[str(s)].get("source") == EXTERNAL]


def protected_kind(op: dict) -> str | None:
    """The protected kind this op claims through ``kind`` or a record ``type``."""
    for field in ("kind", "type"):
        value = str(op.get(field, "")).casefold()
        if value in PROTECTED_KINDS:
            return value
    return None


def needs_local_citation(name: str, op: dict) -> bool:
    return name in AUTHORED_OPS or protected_kind(op) is not None


def op_text(op: dict) -> str:
    """The field that carries this op's content, if it has one."""
    return str(op.get("text") or op.get("alias") or "").strip()


def text_is_cited(op: dict, batch_index: dict[str, dict]) -> bool:
    """True when the op's text appears in one of the items it cites."""
    text = op_text(op).casefold()
    if not text:
        return False
    for _token, item in cited_items(op, batch_index):
        pool = f"{item.get('text', '')} {item.get('evidence', '')}".casefold()
        if text in pool:
            return True
    return False


def needs_cited_text(name: str, op: dict) -> bool:
    return name in CONTENT_OPS or protected_kind(op) is not None


def cited_items(op: dict, batch_index: dict[str, dict]) -> list[tuple[str, dict]]:
    """``(source token, item)`` for every inbox item the op cites."""
    return [(str(s), batch_index[str(s)]) for s in op.get("sources", [])
            if str(s) in batch_index]


def external_rejection(name: str, op: dict, item: dict | None = None,
                       batch_index: dict[str, dict] | None = None) -> str | None:
    """Why external content may not back this op, or None when it may."""
    if name not in EXTERNAL_OPS:
        return "may only add a fact or a new record"
    kind = protected_kind(op)
    if kind is not None:
        return f"may not create a {kind}"
    if str(op.get("trust", "")).casefold() != EXTERNAL:
        return "enters as #external only"
    for fact in op.get("facts") or []:
        if str(fact.get("trust", "")).casefold() != EXTERNAL:
            return "may not store a nested fact under a stronger tag"
        if batch_index is not None and not text_is_cited(
                {"text": fact.get("text", ""), "sources": op.get("sources", [])},
                batch_index):
            return "may not store a nested fact the cited source never said"
    spoken = " ".join(str(x) for x in (
        op.get("text", ""), op.get("evidence", ""),
        (item or {}).get("text", ""), (item or {}).get("evidence", "")))
    if is_imperative(spoken):
        return "hits the imperative filter"
    return None


def validate_plan(store, plan: dict, batch: list[dict], candidates: dict) -> list[str]:
    """Run every §6.4 rule. Returns a list of reject reasons (empty = valid)."""
    reasons: list[str] = []
    max_ops = int(store.section("steward", "max_plan_ops"))

    schema_err = ops_mod.validate_schema(plan, max_ops)
    if schema_err:
        return [schema_err]

    batch_index = {f"inbox:{item['id']}": item for item in batch}
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

        # Trust / imperative / secure rules against the op's declared source
        kind = op.get("kind", "")
        declared = op.get("source", "agent")
        pseudo = {"op": name if name in ("create", "add_fact") else "add_fact",
                  "kind": kind, "source": declared, "trust": op.get("trust", ""),
                  "id": str(op.get("id", "")), "path": "",
                  "evidence": ev, "text": str(op.get("text", ""))}
        safety_err = validate_op_safety(pseudo)
        if safety_err:
            reasons.append(f"{tag}: {safety_err}")

        # Trust gate: the cited inbox item decides, whatever the plan declared.
        # Every reason here names its item, so the pass can dispose exactly it.
        trust = citation_trust(op, batch_index)
        if trust == EXTERNAL:
            for token, item in external_citations(op, batch_index):
                why = external_rejection(name, op, item, batch_index)
                if why:
                    reasons.append(f"trust: {tag} cited {token} is external and {why}")
        elif needs_local_citation(name, op):
            if trust != "local":
                reasons.append(
                    f"trust: {tag} needs a citation from the user or the main assistant")
            elif needs_cited_text(name, op) and not text_is_cited(op, batch_index):
                reasons.append(f"evidence: {tag} text is not in the cited item")

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


def blocked_items(batch: list[dict], reasons: list[str]) -> list[dict]:
    """Items a rejection names, by the ``inbox:<id>`` token its reason carries.

    This is the only reader of that token format, so the contract stays in one
    module: a rule that wants an item disposed puts its token in the reason.
    """
    return [item for item in batch
            if any(f"inbox:{item['id']}" in r for r in reasons)]


def planned_ids(plan: dict, ops: tuple[str, ...] = APPLY_OPS) -> set[str]:
    """Inbox ids cited by ops of the given kinds."""
    return {str(s)[len("inbox:"):]
            for op in plan.get("ops", []) if op.get("op") in ops
            for s in op.get("sources", []) if str(s).startswith("inbox:")}


def rejection_reasons(plan: dict) -> dict[str, str]:
    """Inbox id → the plan's own reason for rejecting it."""
    out: dict[str, str] = {}
    for op in plan.get("ops", []):
        if op.get("op") != "reject":
            continue
        for s in op.get("sources", []):
            if str(s).startswith("inbox:"):
                out[str(s)[len("inbox:"):]] = str(op.get("reason", "rejected"))
    return out


def unapplied_items(plan: dict, batch: list[dict]) -> list[dict]:
    """Proposals no op accounts for. A plan that ignores one has to say so."""
    accounted = planned_ids(plan, tuple(ops_mod.PLAN_OPS))
    rejected = set(rejection_reasons(plan))
    return [{"id": item["id"], "reason": "plan produced no op for this item"}
            for item in batch
            if item["id"] not in accounted and item["id"] not in rejected]


def validate_vault(store, touched: set[str]) -> list[str]:
    """Re-parse every touched file: links resolve and size caps hold."""
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
