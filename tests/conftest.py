"""Shared fixtures: a temp store seeded with the sample vault (plan §4.1)."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from intuition.index import Index            # noqa: E402
from intuition.store import Store            # noqa: E402
from intuition.cli import (SAMPLE_DECISION, SAMPLE_JUNE, SAMPLE_ORG,  # noqa: E402
                           SAMPLE_PROFILE, SAMPLE_WS)


@pytest.fixture
def store(tmp_path: Path) -> Store:
    root = tmp_path / "memory"
    root.mkdir()
    s = Store(str(root))
    s.init_dirs()
    s.ensure_git()
    s.write("shared/PROFILE.md", SAMPLE_PROFILE)
    s.write("shared/person/pers-june.md", SAMPLE_JUNE)
    s.write("shared/workstream/ws-lighthouse.md", SAMPLE_WS)
    s.write("shared/decision/dec-use-fts5.md", SAMPLE_DECISION)
    s.write("shared/org/org-tidewater-labs.md", SAMPLE_ORG)
    s.commit("init: test store")
    return s


@pytest.fixture
def index(store):
    idx = Index(store)
    idx.reindex()
    yield idx
    idx.close()


def seed_raw(store: Store, lines: list[str], day: str = "2026-09-29") -> None:
    p = store.dir(f"raw/{day}.jsonl")
    with open(p, "w") as fh:
        for i, text in enumerate(lines, 1):
            fh.write(json.dumps({
                "ts": f"{day}T10:00:00Z", "host": "hermes", "session": "s-1",
                "role": "user", "text": text,
            }) + "\n")


def seed_inbox(store: Store, items: list[dict]) -> None:
    for item in items:
        full = {
            "id": item.get("id", f"test{i:03d}" if "i" in dir() else "test000"),
            "ts": store.now_iso(), "host": "test", "session": "s-1",
            "agent": item.get("agent", "main"), "task_id": None,
            "kind": item.get("kind", "fact"), "text": item["text"],
            "about": item.get("about"), "source": item.get("source", "user"),
            "evidence": item.get("evidence", item["text"]),
            "confidence": item.get("confidence", 0.9),
        }
        store.append_jsonl("inbox/pending.jsonl", full)
