# Intuition

A daily-driver memory system for long-running AI agents (Hermes Agent, Pi).
Working name from the plan *Cairn* — renamed **Intuition**.

**The rules it never breaks:**

1. **Files are the truth.** Markdown in git is the only source of truth. The
   SQLite FTS5 index is derived — delete `.intuition/` and it rebuilds.
2. **One writer.** Agents read and *propose* into the inbox. Only the Steward
   changes durable memory, one git commit per run.
3. **Every change has a source.** Facts carry trust tags (`#stated`,
   `#observed`, `#inferred:x`, `#external`) and evidence pointers
   (`^raw:<day>#<line>`, `^inbox:<id>`, `^url:…`).
4. **Stated beats inferred.** External content can never become an instruction
   or a preference; instruction-like external text is quarantined.
5. **Stable prompt prefix.** Contract → PROFILE → ONEPAGER → observations →
   NOW → index lines is byte-identical within a session, so provider prompt
   caches hit. Per-turn snippets go after the conversation.
6. **Undo is one command.** `intuition undo` reverts one Steward run.

## Install

```bash
python -m venv .venv && .venv/bin/pip install -e .
intuition init ~/memory          # folders, config, git repo, sample vault
intuition doctor                 # FTS5, git, store, steward mode
intuition schedule               # systemd --user timer, every 15 min
intuition install hermes         # $HERMES_HOME/plugins/intuition/ + shim
intuition install pi             # ~/.pi/agent/extensions/intuition/
```

Then: `hermes config set memory.provider intuition`.

## Day-to-day

```bash
intuition search "who is the pm for lighthouse"
intuition show pers-june          # the raw record
intuition why pers-june           # full evidence chain per fact
intuition note "…" --kind fact --about pers-june
intuition tick --light            # force a steward pass (else: triggers fire)
intuition log / intuition undo HEAD
intuition review                  # quarantine + proposed decisions, weekly
intuition eval evals/retrieval.toml
intuition backup --to /mnt/backup
```

## Architecture

```
Hermes (Python provider) ──┐                     ┌── Pi (TS ext) ⇄ intuition rpc (stdio)
                           ▼                     ▼
        intuition core: model · store · index · search · context · inbox
                           │ reads/writes
                           ▼
                   ~/memory  (git repo: Markdown + JSONL)
                           ▲ the ONLY durable writer
              Steward: light pass (inbox/raw → validated plan → 1 commit)
                       deep pass (expire · profile · aliases · dedupe ·
                                  retention · report; observer/reflector
                                  with a configured model)
```

- **Read path** (`search.py`): multi-query → FTS5 (porter stemming, BM25,
  alias column weighted highest, exact-alias boost) → RRF fusion → one-hop
  link expansion at ×0.3 → pending-inbox matches → budget cap (4 records /
  2400 chars) → zero-hit queries logged as misses; miss→read pairs grow aliases.
- **Write path** (`steward/`): triggers (10 inbox items, 20k raw tokens,
  30 min idle, explicit note, nightly deep) → plan (model via
  `llm_command_*`/HTTP, or deterministic rules) → validation (§6.4: schema,
  verbatim evidence, trust ladder, imperative filter, secure scope, size caps,
  id rules, op budget, no silent deletes) → apply → vault re-validate →
  **one commit or full rollback**.
- **Subagents** get read-only tools and receive memory through the task brief
  (Hermes `delegate_task` children skip providers, so the brief carries the
  decisions + memory prefix + learnings `output_schema` — plan §14 Q1 fallback).

## Tests

```bash
.venv/bin/python -m pytest tests/ -q
```

80 tests cover: grammar round-trips, validity/as-of/expiry, atomic writes,
path traversal, concurrent inbox appends, plan-validator rules (each failing
and passing), steward rollback on injected failure, deep-pass jobs, secure
leak checks, auto-accept policy, RPC end-to-end, triggers, and a retrieval
eval gate. Measured on this machine: **search p95 = 6 ms on a 5,000-record
vault** (target < 50 ms).

## What is stubbed for later

- Observer/reflector/consolidation need a configured model
  (`[steward] llm_command_light/deep` or `llm_http`); without one the Steward
  runs in deterministic mode and skips those jobs.
- The Pi extension ships as a scaffold (Pi is not installed here); it speaks
  the stdio RPC protocol, which is tested end-to-end.
- Phase 0 spike answers are encoded: Hermes provider contract verified against
  the installed host (v2 checkpoint API, `spawn_context_thread`, entry-point
  group `hermes_agent.memory_providers`); children get memory via briefs.
