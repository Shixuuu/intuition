"""Deep pass (plan §6.3): everything in light + condense, profile, aliases,
expiry, dedupe, procedures, retention, report. Deterministic jobs run without
a model; observer/reflector/consolidation run only when one is configured.
"""

from __future__ import annotations

import re
import time

from .. import llm as llm_mod
from ..model import Fact, Record, parse_validity
from . import state as state_mod
from .light import light_pass

# Largest raw window one observer call reads, in characters.
OBSERVER_SEND_CAP = 80000


def deep_pass(store, index, *, reason: str = "nightly") -> dict:
    started = time.time()
    result: dict = {"pass": "deep", "reason": reason, "jobs": {}}

    # light work first so the day's inbox is in the vault before condensing
    result["light"] = light_pass(store, index, reason=f"deep:{reason}")

    jobs = result["jobs"]
    observed: dict[str, int] = {}
    try:
        jobs["expire"] = expire(store)
        jobs["aliases"] = mine_aliases(store, index)
        jobs["dedupe"] = dedupe(store)
        jobs["profile"] = build_onepager(store, index)
        jobs["procedures"] = promote_procedures(store)
        if llm_mod.llm_configured(store):
            message, consumed = observe_raw(store, index)
            jobs["observe"] = message
            observed = consumed
            jobs["condense"] = condense_log(store)
        # retention runs after observation and skips capture nobody has read
        jobs["retention"] = retention(store)
        report(store, result, seconds=time.time() - started, index=index)
        result["committed"] = store.commit(
            f"steward(deep): {len(jobs)} jobs [{reason}]", add_all=True)
    except Exception as e:                     # one fault rolls back every job
        store.rollback(restore_raw=True)
        result["error"] = f"deep jobs failed, rolled back: {e}"
        state_mod.touch(store, light=True)
        return result
    if observed:
        # the observations are committed now, so consuming exactly those bytes is safe
        mark_raw_observed(store, observed)
    state_mod.touch(store, deep=True)
    return result


# -- expiry (Zep validity) ----------------------------------------------------

def expire(store) -> list[str]:
    """Remove facts whose `until` date passed (plan §4.3, §6.3)."""
    today = store.today()
    removed = []
    for rid, rec in store.scan_records().items():
        keep = []
        for f in rec.facts:
            v = parse_validity(f.validity)
            if not v.point and v.end and today > v.end:
                removed.append(f"{rid}: {f.text[:60]}")
                continue
            keep.append(f)
        if len(keep) != len(rec.facts):
            rec.facts = keep
            store.write_record(rec)
    return removed


# -- alias mining --------------------------------------------------------------

def mine_aliases(store, index) -> list[str]:
    """Strong miss→read pairs become aliases (plan §6.3; ≥2 occurrences without
    a model to agree — 1 + model agreement when one is configured)."""
    min_occ = 1 if llm_mod.llm_configured(store) else 2
    added = []
    for query, rid, n in index.alias_candidates():
        if n < min_occ:
            continue
        rec = store.load_record(rid)
        if rec is None or len(rec.aliases) >= 12:
            continue
        alias = query.strip()
        if alias and alias.lower() not in (a.lower() for a in rec.aliases) \
                and alias.lower() != rec.name.lower():
            rec.aliases.append(alias[:48])
            store.write_record(rec)
            added.append(f"{alias} → {rid}")
    index.clear_resolved_misses()
    return added


# -- dedupe (Mem0-style merge, keep one id) ------------------------------------

def dedupe(store) -> list[str]:
    recs = store.scan_records()
    merged = []
    seen: set[str] = set()
    ids = sorted(recs)
    for i, a in enumerate(ids):
        if a in seen:
            continue
        for b in ids[i + 1:]:
            if b in seen:
                continue
            ra, rb = recs[a], recs[b]
            if ra.type != rb.type:
                continue
            overlap = {x.casefold() for x in ra.aliases} & {x.casefold() for x in rb.aliases}
            if len(overlap) >= 2:
                _merge(store, ra, rb)
                seen.add(b)
                merged.append(f"{b} → {a}")
    return merged


def _merge(store, keeper: Record, gone: Record) -> None:
    keeper.facts.extend(gone.facts)
    for alias in gone.aliases + [gone.name]:
        if alias.lower() not in {a.lower() for a in keeper.aliases} \
                and alias.lower() != keeper.name.lower():
            keeper.aliases.append(alias)
    for link in gone.links:
        if (link.rel, link.target) not in [(kept.rel, kept.target) for kept in keeper.links]:
            keeper.links.append(link)
    store.write_record(keeper)
    store.resolve(f"shared/{gone.type}/{gone.id}.md").unlink(missing_ok=True)
    store.invalidate_record_cache()


# -- profile (ONEPAGER) ---------------------------------------------------------

def build_onepager(store, index) -> str:
    """Regenerate ONEPAGER from #stated, #observed, #inferred ≥ 0.8 with ≥ 2
    sources (plan §6.3 profile job, §9.1). Deterministic; no model needed."""
    from ..safety import profile_fact_ok
    cap = int(store.section("profile", "generated_max_chars"))
    min_confidence = float(store.section("safety", "profile_min_confidence"))
    today = store.today()
    lines_by_type: dict[str, list[str]] = {}
    for rid, rec in sorted(store.scan_records().items()):
        for f in rec.current_facts(today):
            if profile_fact_ok(f, min_confidence):
                lines_by_type.setdefault(rec.type, []).append(
                    f"- {rec.name} ({rid}): {f.text} [{f.trust}]")
    parts = ["# One-pager (generated by the Steward — do not hand-edit)\n"]
    for rtype in ("preference", "person", "org", "workstream", "decision",
                  "procedure", "topic"):
        lines = lines_by_type.get(rtype, [])
        if lines:
            parts.append(f"## {rtype.capitalize()}\n" + "\n".join(lines))
    text = "\n\n".join(parts) + "\n"
    if len(text) > cap:
        text = text[:cap] + "\n… (truncated; raise profile.generated_max_chars)\n"
    store.write("shared/ONEPAGER.md", text)
    return f"{len(text)} chars"


# -- procedures promotion --------------------------------------------------------

def promote_procedures(store) -> list[str]:
    """A lesson found by two or more specialists moves to shared/procedure (§4.6)."""
    counts: dict[str, tuple[int, str]] = {}
    agents_dir = store.dir("agents")
    if not agents_dir.exists():
        return []
    for p in sorted(agents_dir.glob("*/PROCEDURES.md")):
        for line in p.read_text().splitlines():
            if line.strip().startswith("- "):
                text = line.strip()[2:]
                key = re.sub(r"\W+", " ", text.casefold()).strip()
                n, _ = counts.get(key, (0, ""))
                counts[key] = (n + 1, text)
    promoted = []
    for key, (n, text) in counts.items():
        if n < 2:
            continue
        rid = f"proc-{abs(hash(key)) % 10**8:08d}"
        if store.load_record(rid) is None:
            today = store.today()
            rec = Record(id=rid, type="procedure",
                         name=text[:60], created=today, updated=today,
                         facts=[Fact(validity=f"since {today[:7]}", text=text,
                                     trust="observed", sources=["tasks:promotion"])])
            store.write_record(rec)
            promoted.append(rid)
    return promoted


# -- retention --------------------------------------------------------------------

def retention(store) -> dict:
    """raw > raw_days once the observer consumed it, tasks > tasks_days,
    inbox archive > archive_months (plan §6.3)."""
    raw_days = int(store.section("retention", "raw_days"))
    tasks_days = int(store.section("retention", "tasks_days"))
    archive_months = int(store.section("retention", "archive_months"))
    now = time.time()
    removed = {"raw": 0, "tasks": 0, "archive": 0, "kept_unobserved": 0}
    state = state_mod.load(store)
    for p in state_mod.raw_files(store):
        if now - p.stat().st_mtime <= raw_days * 86400:
            continue
        if p.stat().st_size > state.raw_offset(p.name):
            removed["kept_unobserved"] += 1      # never drop capture nobody read
            continue
        p.unlink()
        removed["raw"] += 1
    tasks = store.dir("tasks")
    if tasks.exists():
        for p in tasks.iterdir():
            if p.is_dir() and now - p.stat().st_mtime > tasks_days * 86400:
                import shutil
                shutil.rmtree(p, ignore_errors=True)
                removed["tasks"] += 1
    archive = store.dir("inbox/archive")
    if archive_months and archive.exists():
        for p in archive.glob("*.jsonl"):
            if now - p.stat().st_mtime > archive_months * 30 * 86400:
                p.unlink()
                removed["archive"] += 1
    return removed


# -- observer / reflector (model-mode only) ----------------------------------------

def observe_raw(store, index) -> tuple[str, dict[str, int]]:
    """Turn unprocessed raw turns into dated observations (Mastra-style).

    Returns the job's report line and the per-file offsets its text covered, so
    the caller consumes exactly those bytes once the observations are committed.
    """
    from ..llm import call_json
    threshold = int(store.section("observe", "observer_raw_chars"))
    text, offsets = _raw_window(store, OBSERVER_SEND_CAP)
    if len(text) < threshold:
        return "below threshold", {}
    system = ("Turn raw conversation turns into dense dated observations. "
              "Output JSON {\"observations\": [{\"day\": \"YYYY-MM-DD\", "
              "\"priority\": \"high|med|low\", \"text\": \"…\"}]}")
    plan = call_json(store, "deep", system, text)
    n = _write_observations(store, plan.get("observations", []))
    return f"{n} observations", offsets


def condense_log(store) -> str:
    """Condense the observation log past reflector_log_chars (plan §6.3)."""
    from ..llm import call_json
    threshold = int(store.section("observe", "reflector_log_chars"))
    import datetime
    path = store.dir(f"observations/{datetime.date.today():%Y-%m}.md")
    if not path.exists():
        return "no log"
    text = path.read_text()
    if len(text) < threshold:
        return "below threshold"
    system = ("Condense these observations: merge duplicates, drop low-value "
              "lines, keep every fact. Output JSON {\"condensed\": \"markdown\"}.")
    plan = call_json(store, "deep", system, text[:80000])
    if plan.get("condensed"):
        archive = store.dir(f"inbox/archive/obs-{datetime.date.today():%Y-%m}.md")
        archive.parent.mkdir(parents=True, exist_ok=True)
        archive.write_text(text)                      # old wording stays in git too
        path.write_text(plan["condensed"])
        return "condensed"
    return "model returned nothing"


def _write_observations(store, observations: list[dict]) -> int:
    today = store.today()
    path = f"observations/{today[:7]}.md"
    text = store.read_text(path)
    for obs in observations:
        line = f"- [{obs.get('priority', 'med')}] {obs.get('text', '')}"
        day = obs.get("day", today)
        header = f"## {day}"
        if header not in text:
            text = f"{text.rstrip()}\n\n{header}\n{line}\n" if text else f"{header}\n{line}\n"
        else:
            head, _, rest = text.partition(header)
            text = f"{head}{header}\n{line}\n{rest}"
    if observations:
        store.write(path, text)
    return len(observations)


def _raw_window(store, max_chars: int | None = None) -> tuple[str, dict[str, int]]:
    """Unconsumed raw text and, per file, the offset that text reaches.

    With *max_chars* the window is bounded at a line boundary, and only the bytes
    inside it are reported, so a later pass still sees what was left out.
    """
    state = state_mod.load(store)
    chunks: list[str] = []
    offsets: dict[str, int] = {}
    used = 0
    for p in state_mod.raw_files(store):
        data = p.read_bytes()
        done = state.raw_offset(p.name)
        if len(data) <= done:
            continue
        chunk = data[done:]
        if max_chars is not None:
            if used >= max_chars:
                break
            if used + len(chunk) > max_chars:
                chunk = chunk[: max_chars - used]
                cut = chunk.rfind(b"\n")
                if cut > 0:
                    chunk = chunk[: cut + 1]
        used += len(chunk)
        offsets[p.name] = done + len(chunk)
        chunks.append(chunk.decode(errors="replace"))
    return "\n".join(chunks), offsets


def _unprocessed_raw(store) -> str:
    """Raw bytes the observer has not consumed yet, tracked per file."""
    return _raw_window(store)[0]


def mark_raw_observed(store, offsets: dict[str, int] | None = None) -> dict[str, int]:
    """Record consumed raw capture: the given offsets, or every file as consumed."""
    state = state_mod.load(store)
    recorded = state.mark_raw_observed(offsets)
    state.save()
    return recorded


# -- report -------------------------------------------------------------------------

def report(store, result: dict, *, seconds: float, index) -> None:
    today = store.today()
    zero, total = 0, 0
    try:
        zero, total = index.miss_rate()
    except Exception:
        pass
    lines = [
        f"# Steward report — {today}",
        "",
        f"- run: {result.get('pass')} ({result.get('reason')}), {seconds:.1f}s",
        f"- light result: ops={result['light'].get('ops', 0)} "
        f"committed={result['light'].get('committed', '') or '(nothing)'}",
        f"- errors: {result['light'].get('error', 'none')}",
    ]
    for job, out in result.get("jobs", {}).items():
        if job == "light":
            continue
        lines.append(f"- {job}: {out if out else '(nothing to do)'}")
    light = result.get("light", {})
    for item_id, why in (light.get("rejected") or {}).items():
        lines.append(f"- rejected {item_id}: {why}")
    for entry in light.get("unapplied") or []:
        lines.append(f"- unapplied {entry['id']}: {entry['reason']}")
    lines += [
        f"- misses: {zero}/{total} zero-hit",
        f"- quarantine waiting: {len(_quarantine(store))}",
        f"- store size: {sum(1 for _ in store.dir('shared').rglob('*.md'))} records",
    ]
    store.write(f"reports/{today}.md", "\n".join(lines) + "\n")


def _quarantine(store):
    return store.read_jsonl("review/quarantine.jsonl")
