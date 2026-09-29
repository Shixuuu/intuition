"""Intuition — a daily-driver agent memory.

Markdown in git is the truth. FTS5 is the index. The Steward is the only
durable writer. Modules:

    model.py    record + fact grammar            index.py    SQLite FTS5 index
    store.py    paths, locks, atomic git writes  search.py   query → ranked records
    inbox.py    append-only proposals            context.py  prompt blocks
    safety.py   trust ladder + quarantine        llm.py      model calls
    steward/    triggers, plan, validate, apply  rpc.py      stdio server for Pi
"""

from .model import Fact, Link, Record, parse_record, render_record
from .paths import resolve_store

__all__ = ["Fact", "Link", "Record", "parse_record", "render_record", "resolve_store"]

__version__ = "0.1.2"
