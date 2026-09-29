"""Inbox: append-only JSONL proposals from all agents (plan §4.8, §8.3).

Agents propose; only the Steward disposes. Each entry carries host, session,
agent, kind, text, about, source, evidence, confidence. Processed items move
to inbox/archive/<month>.jsonl so pending stays small and searchable.
"""

from __future__ import annotations

import time
import uuid

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
    """Move processed entries to the monthly archive. Returns count archived."""
    items = store.read_jsonl(PENDING)
    keep, done = [], 0
    for item in items:
        if item.get("id") in ids:
            store.append_jsonl(f"inbox/archive/{item['ts'][:7]}.jsonl", item)
            done += 1
        else:
            keep.append(item)
    if done:
        _rewrite(store, keep)
    return done


def quarantine(store, item: dict, reason: str) -> None:
    item = dict(item, quarantine_reason=reason)
    store.append_jsonl("review/quarantine.jsonl", item)


def read_quarantine(store) -> list[dict]:
    return store.read_jsonl("review/quarantine.jsonl")


def _rewrite(store, keep: list[dict]) -> None:
    import json
    p = store.resolve(PENDING)
    tmp = p.with_suffix(".jsonl.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        for item in keep:
            fh.write(json.dumps(item, ensure_ascii=False) + "\n")
        fh.flush()
        import os
        os.fsync(fh.fileno())
    tmp.replace(p)


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def oldest_age_seconds(store) -> float:
    items = read_batch(store)
    if not items:
        return 0.0
    ts = _parse_ts(items[0].get("ts", ""))
    return max(0.0, time.time() - ts) if ts else 0.0


def _parse_ts(iso: str) -> float:
    try:
        return time.mktime(time.strptime(iso, "%Y-%m-%dT%H:%M:%SZ"))
    except (ValueError, TypeError):
        return 0.0
