"""Read path: search, fusion, one-hop, pending inbox, budgets."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .model import parse_iso_ts, tokenise


@dataclass
class Hit:
    id: str
    score: float
    source_query: str = ""
    via: str = ""                # "" | "alias" | "hop:<src>" | "pending"
    fact_lines: list[str] = field(default_factory=list)
    pending: bool = False

    def to_dict(self) -> dict:
        d = {
            "id": self.id, "via": self.via or "search",
            "facts": self.fact_lines,
        }
        if self.pending:
            d["pending"] = True
        return d


def rrf_insert(ranked: dict[str, float], ids: list[str], k: int = 60) -> None:
    """Reciprocal Rank Fusion into `ranked` (glossary: add 1/(k+rank))."""
    for i, rid in enumerate(ids):
        ranked[rid] = ranked.get(rid, 0.0) + 1.0 / (k + i + 1)


def search(store, index, queries: list[str], rtype: str | None = None,
           as_of: str | None = None, limit: int = 8) -> list[Hit]:
    """Fuse every query's hits into one ranking, best first."""
    today = store.today()
    queries = [q for q in queries if q and q.strip()] or [""]
    fused: dict[str, float] = {}
    origin: dict[str, str] = {}
    via_alias: set[str] = set()
    hop_via: dict[str, str] = {}

    for q in queries:
        ranked: list[str] = []
        exact = index.alias_exact(q) if q.strip() else []
        fts = index.fts(q, limit=limit * 3)
        # alias boost: exact whole-query alias hits go first, fixed boost
        ranked.extend(exact)
        ranked.extend(rid for rid, _ in fts if rid not in exact)
        rrf_insert(fused, ranked)
        for i, rid in enumerate(ranked):
            origin.setdefault(rid, q)
            if rid in exact and i == 0:
                via_alias.add(rid)

    if rtype:
        fused = {rid: s for rid, s in fused.items()
                 if _type_of(index, rid) == rtype}

    # one-hop expansion from the top 3. The requested type
    # still applies: a hop must not widen the result set's types.
    hop_decay = float(store.section("search", "hop_decay"))
    top = sorted(fused, key=fused.get, reverse=True)[:3]
    for src in top:
        for nb in index.neighbours(src):
            if nb in fused or (rtype and _type_of(index, nb) != rtype):
                continue
            fused[nb] = fused.get(src, 0.0) * hop_decay
            origin.setdefault(nb, origin.get(src, ""))
            hop_via[nb] = f"hop:{src}"

    hits: list[Hit] = []
    for rid, score in sorted(fused.items(), key=lambda kv: kv[1], reverse=True):
        rec = store.load_record(rid)
        if rec is None:
            continue
        lines = (rec.fact_lines(as_of=as_of, max_lines=3) if as_of
                 else rec.fact_lines(today=today, max_lines=3))
        hits.append(Hit(id=rid, score=score, source_query=origin.get(rid, ""),
                        via=hop_via.get(rid) or ("alias" if rid in via_alias else ""),
                        fact_lines=lines))
        if len(hits) >= limit:
            break

    hits += pending_inbox_hits(store, queries)
    _log_misses(index, queries, hits)
    return hits


def pending_inbox_hits(store, queries: list[str]) -> list[Hit]:
    """Grep inbox/pending.jsonl for the last pending_days days."""
    days = int(store.section("search", "pending_days"))
    cutoff = time.time() - days * 86400
    out: list[Hit] = []
    qtokens = [set(tokenise(q)) for q in queries if q.strip()]
    for item in store.read_jsonl("inbox/pending.jsonl"):
        ts = parse_iso_ts(item.get("ts", ""))
        if ts and ts < cutoff:
            continue
        text = f"{item.get('text', '')} {item.get('evidence', '')}".lower()
        about = item.get("about") or ""
        if any(len(tok & set(text.split())) >= 1 or tok <= set(text.split())
               for tok in (qt for qt in qtokens if qt)) or _mentioned(text, item):
            out.append(Hit(id=about or "inbox", score=0.9, via="pending",
                           fact_lines=[f"{item.get('kind', 'fact')}: {item.get('text', '')}"],
                           pending=True))
    return out[:3]


def _mentioned(text: str, item: dict) -> bool:
    about = (item.get("about") or "").lower()
    return bool(about) and about in text


def _type_of(index, rid: str) -> str:
    return index.type_of(rid)


def _log_misses(index, queries: list[str], hits: list[Hit]) -> None:
    found = {h.source_query for h in hits if h.via in ("", "alias", "search")}
    for q in queries:
        if q.strip() and q not in found:
            index.log_miss(q)


def read_after_miss(index, query: str, rid: str) -> None:
    """Agent read a record right after a zero-hit query — alias candidate."""
    index.log_hit_after_miss(query, rid)


def budget(store, hits: list[Hit]) -> list[Hit]:
    """Cap injection: max_records / max_chars."""
    max_records = int(store.section("search", "max_records"))
    max_chars = int(store.section("search", "max_chars"))
    out, chars = [], 0
    for h in hits:
        block = "\n".join(h.fact_lines)
        if len(out) >= max_records or (out and chars + len(block) > max_chars):
            break
        out.append(h)
        chars += len(block) + 32
    return out


def render_hits(hits: list[Hit], store=None) -> str:
    if not hits:
        return "No matching memories. Say you don't know rather than guessing."
    parts = []
    for h in hits:
        if h.via == "pending":
            parts.append("- (pending — not yet confirmed) " + h.fact_lines[0])
            continue
        lines = "\n".join(f"    {ln}" for ln in h.fact_lines)
        parts.append(f"- {h.id}" + (f"  [{h.via}]" if h.via.startswith("hop") else "")
                     + (f"\n{lines}" if lines else ""))
    return "\n".join(parts)
