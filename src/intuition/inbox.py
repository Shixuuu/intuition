"""Inbox: append-only JSONL proposals from all agents.

Agents propose; only the Steward disposes. Each entry carries host, session,
agent, kind, text, about, source, evidence, confidence. Processed items move
to inbox/archive/<month>.jsonl so pending stays small and searchable.
"""

from __future__ import annotations

import time
import uuid

from .model import parse_iso_ts

KINDS = ("fact", "preference", "decision", "procedure", "question",
         "correction", "forget", "alias")
SOURCES = ("user", "agent", "external")

PENDING = "inbox/pending.jsonl"


def append(store, *, kind: str, text: str, source: str, evidence: str,
           about: str | None = None, confidence: float = 0.9,
           host: str = "local", session: str = "", agent: str = "main",
           task_id: str | None = None, explicit: bool = False) -> dict:
    if kind not in KINDS:
        raise ValueError(f"unknown kind: {kind}")
    if source not in SOURCES:
        raise ValueError(f"unknown source: {source}")
    entry = {
        "id": _new_id(),
        "ts": store.now_iso(),
        "host": host,
        "session": session,
        "agent": agent,
        "task_id": task_id,
        "kind": kind,
        "text": text.strip(),
        "about": about,
        "source": source,
        "evidence": evidence.strip(),
        "confidence": float(confidence),
    }
    if explicit:
        entry["explicit"] = True
    store.append_jsonl(PENDING, entry)
    return entry


def read_batch(store) -> list[dict]:
    return store.read_jsonl(PENDING)


def archive(store, ids: set[str]) -> int:
    """Move processed entries to the monthly archive. Returns count archived.

    Copies to the archive first, then drops the ids under the append lock. A
    crash between the two duplicates an entry in the archive instead of losing
    it, and an entry appended by another agent mid-run stays pending.
    """
    if not ids:
        return 0
    moved = [item for item in store.read_jsonl(PENDING) if item.get("id") in ids]
    for item in moved:
        store.append_jsonl(f"inbox/archive/{item['ts'][:7]}.jsonl", item)
    store.drop_jsonl_ids(PENDING, ids)
    return len(moved)


def restore_pending(store, ids: set[str]) -> int:
    """Put archived entries back in the pending queue after a failed run.

    The archive copy stays, so the worst case is a duplicate archive line rather
    than a proposal that was archived and never applied.
    """
    if not ids:
        return 0
    restored = 0
    archive = store.dir("inbox/archive")
    for path in sorted(archive.glob("*.jsonl")) if archive.exists() else ():
        for item in store.read_jsonl(path.relative_to(store.root)):
            if item.get("id") in ids:
                store.append_jsonl(PENDING, item)
                restored += 1
    return restored


def quarantine(store, item: dict, reason: str) -> None:
    item = dict(item, quarantine_reason=reason)
    store.append_jsonl("review/quarantine.jsonl", item)


def read_quarantine(store) -> list[dict]:
    return store.read_jsonl("review/quarantine.jsonl")


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def oldest_age_seconds(store) -> float:
    items = read_batch(store)
    if not items:
        return 0.0
    ts = parse_iso_ts(items[0].get("ts", ""))
    return max(0.0, time.time() - ts) if ts else 0.0
