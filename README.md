# Intuition

A daily-driver memory system for long-running AI agents (Hermes Agent, Pi).
Working name from the plan *Cairn* — renamed **Intuition**.

**The rules it never breaks:**

1. **Files are the truth.** Markdown in git is the only source of truth. The
   SQLite FTS5 index is derived — delete `.intuition/` and it rebuilds.
2. **One writer.** Agents read and *propose* into the inbox. Only the Steward
   changes durable memory, one commit per pass.
3. **Every change has a source.** Facts carry trust tags (`#stated`,
   `#observed`, `#inferred:x`, `#external`) and evidence pointers
   (`^raw:<day>#<line>`, `^inbox:<id>`, `^url:…`).
4. **Stated beats inferred.** External content can never become an instruction,
   a preference, or a decision. The validator decides this from the cited inbox
   item, whichever planner proposed the op, and the pass quarantines the item it
   refused instead of leaving it to block the batch. A proposal counts as
   authored by "agent" when the main assistant wrote it or a subagent declared it
   as its own in a learnings block; a subagent that read outside material
   declares `source: external` there, and the gate then admits that content only
   as an `#external` fact.
5. **Stable prompt prefix.** Contract → PROFILE → ONEPAGER → observations →
   NOW → index lines is byte-identical within a session, so provider prompt
   caches hit. Per-turn snippets go after the conversation.
6. **Undo is one command.** `intuition undo` reverts one Steward run.

## Install

```bash
python -m venv .venv && .venv/bin/pip install -e .   # or: pipx install intuition-memory
intuition init ~/memory          # folders, config, git repo, sample vault
intuition doctor                 # FTS5, git, store, steward mode
intuition schedule               # systemd --user timer, every 15 min
intuition install hermes         # ~/.hermes/plugins/intuition/ + host discovery
intuition install pi             # ~/.pi/agent/intuition/ + `pi install` registration
```

Then: `hermes config set memory.provider intuition` for Hermes, and Pi picks the
registered package up from its settings.

## Install as a Pi package

The repo root is a Pi package (`keywords: ["pi-package"]` and a `pi` manifest), so
Pi can install it directly:

```bash
pi install /path/to/intuition      # a local checkout; git or npm once published
pi list                            # shows the package
```

The extension then looks for the `intuition` CLI in this order: `INTUITION_BIN`,
the `runtime.json` that `intuition install pi` records, `intuition` on `PATH`, and
`python3 -m intuition.cli`. Inside a session:

```
/intuition init       create the store, which the RPC channel needs first
/intuition            every setting, current value, marker on your changes
/intuition doctor     store, index, git, and Steward mode in one line
/intuition steward.deep_time 04:30
```

`npm publish` from the repo root ships only the extension, its generated
`tools.json`, the readmes, and the licence, which is what the `files` list holds.
The Python side always comes from pip, pipx, or a checkout, because npm cannot
install it.

### Publishing the two artifacts

```bash
tools/publish.sh --check    # preflight only, uploads nothing
npm login && tools/publish.sh --npm     # then: pi install npm:intuition-pi
tools/publish.sh --pypi                 # needs ~/.pypirc or TWINE_PASSWORD
```

The preflight refuses a dirty tree, checks that the three version fields agree,
that the shipped tool manifest matches the schema, and that the tests and lint
pass, then prints what each registry would receive and whether the names are
free. Uploads are irreversible, so run it first. The names are `intuition-pi` on
npm and `intuition-memory` on PyPI, since PyPI's `intuition` was taken; the import
name and the `intuition` command stay `intuition` on every path.

Publishing needs credentials the preflight cannot supply: npm wants either a
granular token with read and write access and "Bypass 2FA" checked, or a live
one-time code (`tools/publish.sh --npm --otp 123456`), and PyPI wants a token in
`~/.pypirc` or `TWINE_PASSWORD`.

## Day-to-day

```bash
intuition search "who is the pm for lighthouse"
intuition show pers-june          # the raw record
intuition why pers-june           # full evidence chain per fact
intuition note "…" --kind fact --about pers-june
intuition note "…" --explicit     # commit on the next tick, past every threshold
intuition tick --light            # force a pass (else: triggers fire)
intuition log / intuition undo HEAD
intuition review                  # quarantine + proposed decisions, weekly
intuition eval evals/retrieval.toml
intuition backup --to /mnt/backup
```

## Configure

Every setting lives in one file, `<store>/intuition.toml`, shared by both hosts.
The keys, their types, their defaults, and a help line each come from
`config.SETTINGS`, so the file written at `init`, the readers, and the command
surfaces cannot disagree.

```bash
intuition config show              # every key, current value, and help line
intuition config show --json
intuition config get steward.idle_minutes
intuition config set steward.idle_minutes 30
intuition config set search.hop_decay 0.5
intuition config help              # the annotated list
```

In Pi, the same thing without leaving the session:

```
/intuition                          # every setting, current value, marker on changes
/intuition steward.deep_time        # one setting, its default, and its help
/intuition steward.deep_time 04:30  # write it; the file changes and the session reloads
/intuition help
```

`/intuition` is a command and deliberately not a tool. The keys include the
safety knobs (`safety.secure_enabled`, `safety.profile_min_confidence`), so only
a person at the keyboard may change them. Tab completion after `/intuition `
offers every key, read from the same schema.

The Steward runs as its own process, so a change applies to the next tick
immediately. A running Pi session reloads its own view on the next write and
otherwise on the next session.

## Architecture

```
Hermes (Python provider) ──┐                     ┌── Pi (TS package) ⇄ intuition rpc (stdio)
                           ▼                     ▼
        intuition core: model · store · index · search · context · inbox
                           │ reads/writes
                           ▼
                   ~/memory  (git repo: Markdown + JSONL)
                           ▲ the ONLY durable writer
              Steward: light pass (inbox → validated plan → 1 commit)
                       deep pass (expire · profile · aliases · dedupe · procedures ·
                                  observe · condense · retention · report)
```

- **Read path** (`search.py`): multi-query → FTS5 (porter stemming, BM25,
  alias column weighted highest, exact-alias boost) → RRF fusion → one-hop
  link expansion at ×0.3 (inside the type filter, so a hop cannot widen the
  requested type) → pending-inbox matches → budget cap (4 records /
  2400 chars) → zero-hit queries logged as misses; miss→read pairs grow aliases.
- **Write path** (`steward/`): triggers → plan (model via `llm_command_*`/HTTP,
  or deterministic rules) → validation (schema, verbatim evidence, trust ladder
  resolved from the cited item, imperative filter, secure scope, size caps, id
  rules, op budget, no silent deletes) → apply to **copies** → vault re-validate →
  archive inside the transaction → **one commit or a path-scoped rollback**.
- **Triggers** (`steward/tick.py`, `steward/state.py`): 10 inbox items, an
  explicit note older than 5 minutes, unobserved raw capture past
  `light_raw_chars`, the oldest proposal past `pending_age_minutes`, 30 minutes
  of quiet, a session that ends holding proposals no pass has planned, and a
  nightly deep pass after `deep_time` once per local day. Each trigger is
  consumed by the pass it fires: the idle clock restarts on any pass and the
  attempted batch is remembered, so nothing fires forever on the same state.
- **Raw pipeline**: turns land in `raw/<day>.jsonl` and never enter a prompt.
  The observer records consumption per file, so turns appended later are still
  unprocessed, and retention only deletes capture the observer has read.
- **Subagents** get read-only tools and receive memory through the task brief
  (Hermes `delegate_task` children skip providers, so the brief carries the
  decisions + memory prefix + learnings `output_schema` — plan §14 Q1 fallback).

## Budgets

Every prompt block is bounded by a **character** budget, named for that unit:

| Key | Default | Block |
| --- | --- | --- |
| `profile.pinned_max_chars` | 4000 | hand-written PROFILE.md |
| `profile.generated_max_chars` | 12000 | generated ONEPAGER.md |
| `observe.prefix_max_chars` | 16000 | observation tail |
| `search.max_chars` / `max_records` | 2400 / 4 | per-turn snippet |
| `model.NOW_MAX_CHARS` / `BRIEF_MAX_CHARS` | 4000 / 8000 | NOW.md, task briefs |

Earlier releases spelled these `*_tokens` and multiplied by four at each use
site; the values above are the same budgets in characters. If you overrode a
`*_tokens` key in `intuition.toml`, rename it and quadruple the number.

## Tests

```bash
.venv/bin/python -m pytest tests/ -q     # 136 tests
.venv/bin/ruff check src tests           # the committed lint config
intuition init "$TMP/memory" && intuition --store "$TMP/memory" eval evals/retrieval.toml
```

Covered: grammar round-trips, validity/as-of/expiry, atomic writes, path
traversal, concurrent inbox appends (including a note that lands mid-archive),
plan-validator rules each failing and passing, external content refused at the
gate for procedures/preferences/decisions, fault injection in the light pass and
in the deep jobs with the vault and the in-process view both checked, raw-pipeline
continuity and retention, trigger consumption and session-end promptness, the
schema single source, the config writer and its command surfaces, RPC end-to-end,
and the retrieval eval gate.

CI (`.github/workflows/ci.yml`) runs the lint config, the suite, and the eval
gate against a fresh store. Measured on this machine: **search p95 = 6 ms on a
5,000-record vault** (target < 50 ms), and the prefix costs ~1.0k tokens at
5,000 records because the index block is capped at 40 lines.

## What is stubbed for later

- Observer/reflector/consolidation need a configured model
  (`[steward] llm_command_light/deep` or `llm_http`); without one the Steward
  runs in deterministic mode and skips those jobs.
- The Pi package ships `extensions/intuition/index.ts` plus a generated
  `tools.json`. Pi runs TypeScript through jiti, so no build step is needed.
