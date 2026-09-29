"""Shared fixtures: a temp store seeded with the sample vault."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from intuition.cli import (  # noqa: E402
    SAMPLE_DECISION,
    SAMPLE_JUNE,
    SAMPLE_ORG,
    SAMPLE_PROFILE,
    SAMPLE_WS,
)
from intuition.index import Index  # noqa: E402
from intuition.store import Store  # noqa: E402


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


@pytest.fixture
def raw_capture(store):
    """Append raw conversation turns to raw/<day>.jsonl, as a host session does."""
    def seed(lines: list[str], day: str = "2026-09-29") -> Path:
        p = store.dir(f"raw/{day}.jsonl")
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a") as fh:
            for text in lines:
                fh.write(json.dumps({
                    "ts": f"{day}T10:00:00Z", "host": "hermes", "session": "s-1",
                    "role": "user", "text": text,
                }) + "\n")
        return p
    return seed



