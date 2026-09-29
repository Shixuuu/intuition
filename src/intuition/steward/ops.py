"""Steward op application (plan §6.2 step 6, Appendix C.4).

Ops: create · add_fact · close_fact · correct · add_alias · link · set_prose ·
procedure_add · decision_propose · observe · remove_fact (forget only) ·
noop · reject. Every op cites evidence; apply mutates records in memory and
returns the set of touched record ids. Files are written by the light pass.
"""

from __future__ import annotations

import time

from .. import inbox as inbox_mod
from ..model import (Fact, Link, RECORD_TYPES, close_fact, make_id,
                     parse_record, render_record)

PLAN_OPS = (
    "create", "add_fact", "close_fact", "correct", "add_alias", "link",
    "set_prose", "procedure_add", "decision_propose", "observe",
    "remove_fact", "noop", "reject",
)

_REQUIRED = {
    "create": ("id", "type", "name", "evidence"),
    "add_fact": ("id", "validity", "text", "trust", "evidence"),
    "close_fact": ("id", "match", "end", "evidence"),
    "correct": ("id", "match", "text", "evidence"),
    "add_alias": ("id", "alias", "evidence"),
    "link": ("id", "relation", "target", "evidence"),
    "set_prose": ("id", "text", "evidence"),
    "procedure_add": ("agent", "text", "evidence"),
    "decision_propose": ("name", "text", "evidence"),
    "observe": ("text", "evidence"),
    "remove_fact": ("id", "match", "evidence"),
    "noop": (), "reject": ("reason",),
}


def validate_schema(plan: dict, max_ops: int) -> str | None:
    """plan §6.4 rules 'Schema' + 'Budget'. Returns reject reason or None."""
    ops = plan.get("ops")
    if not isinstance(ops, list):
        return "schema: plan has no ops list"
    if len(ops) > max_ops:
        return f"budget: {len(ops)} ops > max_plan_ops {max_ops}; split the batch"
    for op in ops:
        name = op.get("op", "")
        if name not in PLAN_OPS:
            return f"schema: unknown op {name!r}"
        missing = [f for f in _REQUIRED[name] if not op.get(f)]
        if missing:
            return f"schema: {name} missing {missing}"
        if name != "noop" and not op.get("sources") and name != "reject":
            return f"schema: {name} missing sources"
    return None


def apply_plan(store, plan: dict, batch: list[dict]) -> tuple[set[str], list[str]]:
    """Apply a validated plan. Returns (touched record ids, notes).
    Raises on the first inconsistency; the caller rolls back the git tree."""
    recs = store.scan_records()
    today = store.today()
    touched: set[str] = set()
    notes: list[str] = []

    for op in plan.get("ops", []):
        name = op["op"]
        if name in ("noop", "reject"):
            continue
        if name == "observe":
            _observe(store, op, batch, today)
            continue
        if name == "procedure_add":
            _procedure_add(store, op)
            continue
        if name == "decision_propose":
            rid = _decision_propose(store, op, today)
            recs[rid] = store.load_record(rid)
            touched.add(rid)
            notes.append(f"decision proposed: {rid}")
            continue

        rid = op.get("id", "")
        rec = recs.get(rid)
        if name == "create":
            if rec is not None:
                raise ValueError(f"id: {rid} already exists")
            rtype = op.get("type", "")
            if rtype not in RECORD_TYPES:
                raise ValueError(f"schema: unknown record type {rtype!r}")
            rec = _create_record(store, op, today)
            recs[rec.id] = rec
            touched.add(rec.id)
            notes.append(f"created {rec.id}")
            continue
        if rec is None:
            raise ValueError(f"id: unknown record {rid!r}")

        if name == "add_fact":
            rec.facts.append(Fact(
                validity=op["validity"], text=op["text"], trust=op.get("trust", "observed"),
                sources=list(op.get("sources", [])),
                confidence=op.get("confidence")))
        elif name == "close_fact":
            if not close_fact(rec, op["match"], op["end"], op.get("sources", [])):
                raise ValueError(f"close_fact: no open fact matches {op['match']!r} in {rid}")
        elif name == "correct":
            close_fact(rec, op["match"], today, op.get("sources", []))
            rec.facts.append(Fact(
                validity=op.get("validity", f"since {today[:7]}"), text=op["text"],
                trust=op.get("trust", "stated"), sources=list(op.get("sources", []))))
        elif name == "add_alias":
            alias = op["alias"].strip()
            if alias and alias.lower() not in (a.lower() for a in rec.aliases) \
                    and alias.lower() != rec.name.lower() and len(rec.aliases) < 12:
                rec.aliases.append(alias)
        elif name == "link":
            if op["relation"] not in (
                    "works-at", "manages", "reports-to", "member-of", "owns",
                    "supplies", "depends-on", "related-to", "decided-in", "supersedes"):
                raise ValueError(f"schema: unknown link relation {op['relation']!r}")
            rec.links.append(Link(op["relation"], op["target"], op.get("validity", "")))
        elif name == "set_prose":
            rec.prose = [ln for ln in op["text"].split("\n") if ln.strip()][:8]
        elif name == "remove_fact":
            before = len(rec.facts)
            rec.facts = [f for f in rec.facts if op["match"].lower() not in f.text.lower()]
            if len(rec.facts) == before:
                raise ValueError(f"remove_fact: no fact matches {op['match']!r} in {rid}")
            notes.append(f"forget: removed from {rid}")
        else:
            raise ValueError(f"schema: unknown op {name!r}")
        touched.add(rid)

    for rid in touched:
        store.write_record(recs[rid])
    return touched, notes


def _create_record(store, op, today: str):
    from ..model import Record
    rec = Record(
        id=op["id"], type=op["type"], name=op["name"],
        aliases=[op["alias"]] if op.get("alias") else [],
        created=today, updated=today,
        prose=[op["text"]] if op.get("text") else [])
    if op.get("facts"):
        for f in op["facts"]:
            rec.facts.append(Fact(
                validity=f.get("validity", f"since {today[:7]}"),
                text=f["text"], trust=f.get("trust", "observed"),
                sources=list(op.get("sources", []))))
    return rec


def _observe(store, op, batch: list[dict], today: str) -> None:
    month = today[:7]
    path = f"observations/{month}.md"
    text = store.read_text(path)
    prio = {"decision": "high", "preference": "med"}.get(op.get("kind", ""), "med")
    src = " ".join(f"^{s}" for s in op.get("sources", []))
    line = f"- [{prio}] {op['text']} {src}".rstrip()
    header = f"## {today}"
    if header not in text:
        text = f"{text.rstrip()}\n\n{header}\n{line}\n" if text else f"{header}\n{line}\n"
    else:
        head, _, rest = text.partition(header)
        text = f"{head}{header}\n{line}\n{rest}"
    store.write(path, text)


def _procedure_add(store, op) -> None:
    agent = op["agent"].replace("/", "-")
    path = f"agents/{agent}/PROCEDURES.md"
    text = store.read_text(path)
    if not text:
        text = f"# Procedures: {agent}\n\n## Lessons\n"
    if op["text"] not in text:
        store.write(path, text.rstrip() + f"\n- {op['text']}\n")


def _decision_propose(store, op, today: str) -> str:
    from ..model import Record
    rid = make_id("decision", op["name"])
    rec = Record(
        id=rid, type="decision", name=op["name"], created=today, updated=today,
        front={"status": "proposed", "decided": today, "decided_by": op.get("decided_by", "main")},
        decision={
            "Decision": [op["text"]],
            "Why": [op.get("why", "")] if op.get("why") else [],
            "Revisit if": [op["revisit"]] if op.get("revisit") else [],
        })
    store.write_record(rec)
    return rid
