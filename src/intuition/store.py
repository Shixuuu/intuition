"""Files, locks, atomic writes, git — the durable layer (plan §3.2, §9.5).

Rules encoded here:
  * every path must stay inside the store (path-traversal check),
  * writes are temp-file + fsync + rename (atomic),
  * JSONL appends take an flock and fsync (concurrent readers safe),
  * git is the transaction: one commit per Steward run, rollback = reset --hard.
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import tempfile
import time
import tomllib
from contextlib import contextmanager
from pathlib import Path

from .model import Record, parse_record, render_record
from .paths import DIR_NAMES, GITIGNORE, resolve_store

CONFIG_NAME = "intuition.toml"
STATE_DIR = ".intuition"
DEFAULT_CONFIG = """\
[store]
path = "{path}"

[search]
max_records = 4
max_chars = 2400
hop_decay = 0.3
pending_days = 2

[profile]
pinned_max_tokens = 1000
generated_max_tokens = 3000
index_lines = 40

[observe]
observer_raw_tokens = 20000
reflector_log_tokens = 12000
prefix_max_tokens = 4000

[steward]
tick_minutes = 15
light_inbox_items = 10
light_raw_tokens = 20000
idle_minutes = 30
deep_time = "03:00"
llm_command_light = ""
llm_command_deep = ""
max_plan_ops = 60

[retention]
raw_days = 30
tasks_days = 14
archive_months = 12

[safety]
quarantine_external_imperatives = true
profile_min_confidence = 0.8
secure_enabled = false
"""


class StoreError(RuntimeError):
    pass


class Store:
    def __init__(self, path: str | None = None):
        self.root = resolve_store(path)
        if not self.root.exists():
            raise StoreError(f"store not found: {self.root} (run `intuition init`)")
        self.cfg = self._load_config()
        self._rec_cache: dict[str, Record] | None = None
        self._rec_cache_mt: float | None = None
        self._walk_cache: tuple[float, float] | None = None

    # -- config ------------------------------------------------------------

    def _load_config(self) -> dict:
        f = self.root / CONFIG_NAME
        if not f.exists():
            return {}
        return tomllib.loads(f.read_text())

    def section(self, name: str, key: str, default):
        return self.cfg.get(name, {}).get(key, default)

    # -- paths -------------------------------------------------------------

    def dir(self, rel: str) -> Path:
        return self.root / rel

    def resolve(self, rel: str | Path) -> Path:
        """Join + verify the path stays inside the store (plan §9.5)."""
        p = (self.root / rel).resolve()
        if self.root.resolve() not in p.parents and p != self.root.resolve():
            raise StoreError(f"path escapes store: {rel}")
        return p

    # -- init / structure ---------------------------------------------------

    def init_dirs(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        for rel in DIR_NAMES:
            self.dir(rel).mkdir(parents=True, exist_ok=True)
        cfg = self.root / CONFIG_NAME
        if not cfg.exists():
            cfg.write_text(DEFAULT_CONFIG.format(path=self.root))
        gi = self.root / ".gitignore"
        if not gi.exists():
            gi.write_text(GITIGNORE)

    def ensure_git(self) -> None:
        if (self.root / ".git").exists():
            return
        self.git("init", "-q")
        if not self._has_global_git_user():
            self.git("config", "user.name", "intuition")
            self.git("config", "user.email", "intuition@local")

    def _has_global_git_user(self) -> bool:
        try:
            name = self.git("config", "--global", "user.name").strip()
            email = self.git("config", "--global", "user.email").strip()
            return bool(name and email)
        except StoreError:
            return False

    # -- atomic writes -------------------------------------------------------

    def write(self, rel: str | Path, data: str) -> Path:
        p = self.resolve(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=f".{p.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, p)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        self.invalidate_record_cache()
        return p

    @contextmanager
    def lock(self, name: str):
        """Advisory exclusive lock under .intuition/locks/."""
        lf = self.dir(f"{STATE_DIR}/locks/{name}.lock")
        lf.parent.mkdir(parents=True, exist_ok=True)
        fh = open(lf, "w")
        try:
            fcntl.flock(fh, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
            fh.close()

    def append_jsonl(self, rel: str | Path, obj: dict) -> None:
        """Whole-line append under flock + fsync (plan §8.3)."""
        p = self.resolve(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(obj, ensure_ascii=False, sort_keys=False)
        with self.lock("jsonl-" + p.name.replace(".", "-")):
            with open(p, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
                fh.flush()
                os.fsync(fh.fileno())

    def read_jsonl(self, rel: str | Path) -> list[dict]:
        p = self.resolve(rel)
        if not p.exists():
            return []
        out = []
        for i, line in enumerate(p.read_text().splitlines(), 1):
            if not line.strip():
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError as e:
                raise StoreError(f"{p}:{i}: bad JSON: {e}") from e
        return out

    # -- records --------------------------------------------------------------

    def vault_mtime(self) -> float:
        """Newest mtime under shared/ — cheap walk for cache validation (§5.3).
        Memoised for 250 ms; every Store.write invalidates immediately."""
        now = time.monotonic()
        walk = self.__dict__.get("_walk_cache")
        if walk and now - walk[0] < 0.25:
            return walk[1]
        latest = 0.0
        shared = self.dir("shared")
        if shared.exists():
            for dirpath, dirnames, filenames in os.walk(shared):
                for fn in filenames:
                    if fn.endswith(".md"):
                        m = os.stat(os.path.join(dirpath, fn)).st_mtime
                        if m > latest:
                            latest = m
        self._walk_cache = (now, latest)
        return latest

    def scan_records(self) -> dict[str, Record]:
        mt = self.vault_mtime()
        recs = self.__dict__.get("_rec_cache")
        if recs is not None and self.__dict__.get("_rec_cache_mt") == mt:
            return recs
        out: dict[str, Record] = {}
        for p in sorted(self.dir("shared").rglob("*.md")):
            if p.name in ("PROFILE.md", "ONEPAGER.md"):
                continue
            rec = parse_record(p.read_text(), path=str(p))
            if rec.id:
                out[rec.id] = rec
        self._rec_cache = out
        self._rec_cache_mt = mt
        return out

    def invalidate_record_cache(self) -> None:
        self._rec_cache = None
        self._rec_cache_mt = None
        self._walk_cache = None

    def load_record(self, rec_id: str) -> Record | None:
        recs = self.scan_records()
        rec = recs.get(rec_id)
        if rec:
            return rec
        wanted = rec_id.lower()
        for rec in recs.values():
            if rec.name.lower() == wanted or wanted in (a.lower() for a in rec.aliases):
                return rec
        return None

    def write_record(self, rec: Record) -> Path:
        rec.updated = rec.updated or self.today()
        rel = f"shared/{rec.type}/{rec.id}.md"
        return self.write(rel, render_record(rec))

    def read_text(self, rel: str | Path) -> str:
        p = self.resolve(rel)
        return p.read_text() if p.exists() else ""

    def today(self) -> str:
        return time.strftime("%Y-%m-%d")

    def now_iso(self) -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    # -- git --------------------------------------------------------------------

    def git(self, *args: str, check: bool = True) -> str:
        r = subprocess.run(
            ["git", "-C", str(self.root), *args],
            capture_output=True, text=True,
        )
        if check and r.returncode != 0:
            raise StoreError(f"git {' '.join(args)}: {r.stderr.strip()}")
        return r.stdout

    def dirty(self) -> bool:
        return bool(self.git("status", "--porcelain").strip())

    def commit(self, msg: str, add_all: bool = False) -> str:
        self.git("add", "-A" if add_all else ".")
        if not self.dirty():
            return ""
        self.git("commit", "-q", "-m", msg)
        return self.git("rev-parse", "--short", "HEAD").strip()

    def rollback(self) -> None:
        """Abort the current run: tree back to HEAD (plan §6.2 step 8)."""
        self.git("reset", "--hard", "HEAD", check=False)
        self.git("clean", "-fd", check=False)

    def head(self) -> str:
        return self.git("rev-parse", "--short", "HEAD", check=False).strip() or "(empty)"

    def commit_message(self, ref: str = "HEAD") -> str:
        return self.git("log", "-1", "--format=%s", ref).strip()

    def log_messages(self, n: int = 20) -> list[tuple[str, str]]:
        out = self.git("log", f"-{n}", "--format=%h\t%s", check=False)
        rows = []
        for line in out.splitlines():
            if "\t" in line:
                h, msg = line.split("\t", 1)
                rows.append((h, msg))
        return rows
