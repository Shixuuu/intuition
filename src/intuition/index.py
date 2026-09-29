"""SQLite FTS5 index — derived, always rebuildable.

Tables:
  docs_fts   FTS5, porter tokenizer, columns (name, aliases, headings, facts, prose)
  links      (src, dst, relation)
  misses     (query, ts, found_id)  — zero-hit queries and later query→read pairs
  usage      (id, count, last)      — index-line ranking
  meta       (key, value)           — last build, max mtime

WAL mode so readers never block. Deleting .intuition/ is always safe.
"""

from __future__ import annotations

import functools
import sqlite3
import threading
import time

from .model import parse_record

WEIGHTS = "0, 6, 8, 3, 2, 1"    # bm25 weights per column: id 0, name 6, aliases 8, headings 3, facts 2, prose 1


def _serialized(method):
    """One lock per Index. A host can dispatch tool calls from a different thread
    than the one that built the connection, and sqlite connections are not safe
    for concurrent use, so every database call goes through the index's lock."""
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapper

SCHEMA = """
CREATE TABLE IF NOT EXISTS docs (
    id TEXT PRIMARY KEY, type TEXT, mtime REAL
);
CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5(
    doc_id UNINDEXED, name, aliases, headings, facts, prose,
    tokenize='porter unicode61'
);
CREATE TABLE IF NOT EXISTS links (src TEXT, dst TEXT, relation TEXT);
CREATE TABLE IF NOT EXISTS misses (
    query TEXT, ts TEXT, found_id TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS usage (id TEXT PRIMARY KEY, count INTEGER, last REAL);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def headings_text(rec) -> str:
    heads = [f"# {rec.name}", "## Facts", "## Links"]
    heads += [f"## {h}" for h in rec.decision]
    return " ".join(heads)


class Index:
    def __init__(self, store):
        self.store = store
        self.path = store.dir(".intuition/index.sqlite")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
        self._lock = threading.RLock()
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self) -> None:
        with self._lock:
            self.db.close()

    # -- build / stale check ------------------------------------------------

    def max_mtime(self) -> float:
        latest = 0.0
        shared = self.store.dir("shared")
        for p in shared.rglob("*.md") if shared.exists() else ():
            latest = max(latest, p.stat().st_mtime)
        return latest

    @_serialized
    def is_stale(self) -> bool:
        row = self.db.execute("SELECT value FROM meta WHERE key='max_mtime'").fetchone()
        return row is None or float(row["value"]) != self.max_mtime()

    @_serialized
    def update(self) -> int:
        """Update changed files only. Returns count of (re)indexed docs."""
        latest = self.max_mtime()
        changed = 0
        rows = {
            r["id"]: r for r in self.db.execute(
                "SELECT id, mtime FROM docs").fetchall()
        }
        seen = set()
        for p in sorted(self.store.dir("shared").rglob("*.md")):
            if p.name in ("PROFILE.md", "ONEPAGER.md"):
                continue
            mtime = p.stat().st_mtime
            rec = parse_record(p.read_text(), path=str(p))
            if not rec.id:
                continue
            seen.add(rec.id)
            old = rows.get(rec.id)
            if old and abs(old["mtime"] - mtime) < 0.001:
                continue
            self._index_doc(rec, mtime)
            changed += 1
        # drop docs whose files vanished
        for rid in set(rows) - seen:
            self._remove_doc(rid)
            changed += 1
        self.db.execute(
            "INSERT INTO meta(key,value) VALUES('max_mtime',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (repr(latest),))
        self.db.commit()
        return changed

    @_serialized
    def reindex(self) -> int:
        for tbl in ("docs_fts", "docs", "links"):
            self.db.execute(f"DELETE FROM {tbl}")
        self.db.execute("DELETE FROM meta WHERE key='max_mtime'")
        self.db.commit()
        return self.update()

    @_serialized
    def _index_doc(self, rec, mtime: float) -> None:
        aliases = ", ".join([rec.name, *rec.aliases])
        facts = "\n".join(f.line() for f in rec.facts)
        prose = " ".join(rec.prose) + " " + " ".join(
            " ".join(v) for v in rec.decision.values())
        self.db.execute("DELETE FROM docs_fts WHERE doc_id=?", (rec.id,))
        self.db.execute(
            "INSERT INTO docs_fts(doc_id,name,aliases,headings,facts,prose) "
            "VALUES(?,?,?,?,?,?)",
            (rec.id, rec.name, aliases, headings_text(rec), facts, prose))
        self.db.execute(
            "INSERT INTO docs(id,type,mtime) VALUES(?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET type=excluded.type, mtime=excluded.mtime",
            (rec.id, rec.type, mtime))
        self.db.execute("DELETE FROM links WHERE src=?", (rec.id,))
        for link in rec.links:
            self.db.execute(
                "INSERT INTO links(src,dst,relation) VALUES(?,?,?)",
                (rec.id, link.target, link.rel))

    @_serialized
    def _remove_doc(self, rid: str) -> None:
        self.db.execute("DELETE FROM docs_fts WHERE doc_id=?", (rid,))
        self.db.execute("DELETE FROM docs WHERE id=?", (rid,))
        self.db.execute("DELETE FROM links WHERE src=?", (rid,))

    # -- query ----------------------------------------------------------------

    @_serialized
    def fts(self, query: str, limit: int = 10) -> list[tuple[str, float]]:
        """Ranked (id, score) — score is fts5 rank, lower (more negative) = better."""
        q = _fts_query(query)
        if not q:
            return []
        try:
            rows = self.db.execute(
                f"SELECT doc_id AS id, bm25(docs_fts, {WEIGHTS}) AS rank "
                "FROM docs_fts "
                "WHERE docs_fts MATCH ? ORDER BY rank LIMIT ?",
                (q, limit)).fetchall()
        except sqlite3.OperationalError:
            return []
        return [(r["id"], r["rank"]) for r in rows]

    @_serialized
    def alias_exact(self, query: str) -> list[str]:
        """Whole-query alias matches, boosted so they outrank a fuzzy hit."""
        wanted = query.strip().casefold()
        if not wanted:
            return []
        rows = self.db.execute("SELECT doc_id AS id, aliases FROM docs_fts").fetchall()
        hits = []
        for r in rows:
            names = {n.strip().casefold() for n in r["aliases"].split(",") if n.strip()}
            if wanted in names:
                hits.append(r["id"])
        return hits

    @_serialized
    def neighbours(self, rid: str) -> list[str]:
        rows = self.db.execute(
            "SELECT dst FROM links WHERE src=? UNION "
            "SELECT src FROM links WHERE dst=?", (rid, rid)).fetchall()
        return [r[0] for r in rows if r[0]]

    @_serialized
    def type_of(self, rid: str) -> str:
        row = self.db.execute("SELECT type FROM docs WHERE id=?", (rid,)).fetchone()
        return row["type"] if row else ""

    @_serialized
    def clear_resolved_misses(self) -> None:
        """Drop miss rows that grew an alias."""
        self.db.execute("DELETE FROM misses WHERE found_id != ''")
        self.db.commit()

    # -- bookkeeping ------------------------------------------------------------

    @_serialized
    def record_usage(self, rid: str) -> None:
        self.db.execute(
            "INSERT INTO usage(id,count,last) VALUES(?,1,?) "
            "ON CONFLICT(id) DO UPDATE SET count=count+1, last=excluded.last",
            (rid, time.time()))
        self.db.commit()

    @_serialized
    def log_miss(self, query: str) -> None:
        self.db.execute(
            "INSERT INTO misses(query,ts) VALUES(?,?)",
            (query, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())))
        self.db.commit()

    @_serialized
    def log_hit_after_miss(self, query: str, rid: str) -> None:
        """Agent searched (miss) then read a record — a candidate alias."""
        row = self.db.execute(
            "SELECT rowid FROM misses WHERE query=? AND found_id='' "
            "ORDER BY rowid DESC LIMIT 1", (query,)).fetchone()
        if row:
            self.db.execute("UPDATE misses SET found_id=? WHERE rowid=?", (rid, row["rowid"]))
        else:
            self.db.execute(
                "INSERT INTO misses(query,ts,found_id) VALUES(?,?,?)",
                (query, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), rid))
        self.db.commit()

    @_serialized
    def alias_candidates(self) -> list[tuple[str, str, int]]:
        """Strong miss→read pairs, grouped: (query, record id, occurrences)."""
        rows = self.db.execute(
            "SELECT query, found_id, COUNT(*) AS n FROM misses "
            "WHERE found_id != '' GROUP BY query, found_id HAVING n >= 1").fetchall()
        return [(r["query"], r["found_id"], r["n"]) for r in rows]

    @_serialized
    def miss_rate(self) -> tuple[int, int]:
        total = self.db.execute("SELECT COUNT(*) FROM misses").fetchone()[0]
        zero = self.db.execute("SELECT COUNT(*) FROM misses WHERE found_id=''").fetchone()[0]
        return zero, total

    @_serialized
    def top_used(self, limit: int = 40) -> list[str]:
        rows = self.db.execute(
            "SELECT id FROM usage ORDER BY last DESC LIMIT ?", (limit,)).fetchall()
        return [r["id"] for r in rows]


def _fts_query(text: str) -> str:
    """Build a safe OR-of-terms FTS5 query (no operators from user input)."""
    terms = []
    for tok in text.replace('"', " ").split():
        tok = "".join(c for c in tok if c.isalnum() or c in "-_'")
        if tok and not tok[0].isdigit():
            terms.append(f'"{tok}"')
    return " OR ".join(terms)
