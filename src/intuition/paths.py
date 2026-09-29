"""Store paths and default resolution."""

from __future__ import annotations

import os
from pathlib import Path


def default_store() -> Path:
    env = os.environ.get("INTUITION_STORE")
    if env:
        return Path(env).expanduser()
    cfg = Path("~/.config/intuition/store").expanduser()
    if cfg.exists():
        text = cfg.read_text().strip()
        if text:
            return Path(text).expanduser()
    return Path("~/memory").expanduser()


def resolve_store(path: str | None = None) -> Path:
    if path:
        return Path(path).expanduser().resolve()
    return default_store().resolve()


DIR_NAMES = (
    "shared/person", "shared/org", "shared/preference", "shared/topic",
    "shared/decision", "shared/workstream", "shared/procedure",
    "agents", "tasks", "working", "observations", "timeline/daily",
    "timeline/weekly", "inbox/archive", "raw", "review", "secure",
    "reports", ".intuition/locks",
)

GITIGNORE = ".intuition/\n"
