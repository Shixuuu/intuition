# Intuition — Pi extension

This directory is what the installed package runs. It is committed with a
generated `tools.json`, so a plain `pi install <repo>` works with no Python-side
step first. `intuition install pi` copies it to `~/.pi/agent/intuition/`, writes
`runtime.json` (the interpreter that has the CLI), and registers it with Pi so
`pi list` shows it.

The extension finds the `intuition` CLI in this order: `INTUITION_BIN`, the
`runtime.json` an install recorded, `intuition` on `PATH`, then
`python3 -m intuition.cli`. It spawns one `intuition rpc` process per session
(JSON lines over stdio), registers the memory tools from `tools.json`, builds the
stable prompt prefix once per session and again after compaction, captures
main-session turns into `raw/`, and asks the Steward for a session-end pass.

That pass plans on the session's own model: the Steward asks the extension for a
completion over the same channel and the extension runs it through
`ctx.modelRegistry` with whatever provider, model, and credentials the session is
using. No model settings are needed, and `steward.llm_mode` overrides the
route.

Tool surface: `memory_search` and `memory_read` for children, plus
`memory_note`, `memory_forget`, `memory_now`, `memory_brief`, `memory_learn`,
`memory_timeline`, `memory_secure_get` and `memory_status` for the main session.

## Configuring the memory system from Pi

```
/intuition init                     create the store, so the RPC channel can start
/intuition doctor                   check the store, index, git, and Steward mode
/intuition tick                     force a Steward pass
/intuition                          the inherited model, every setting, and changes
/intuition steward.deep_time        one setting, its default, and its help
/intuition steward.deep_time 04:30  write it to <store>/intuition.toml
/intuition help
```

Settings are the same keys `intuition config show` prints, read from the
package's schema over the RPC channel. The command is registered separately from
the tools on purpose: the keys include the safety knobs, so the model must not be
able to change them.

Child detection: set `INTUITION_ROLE=subagent` and `INTUITION_AGENT=<name>` in
the child process env when spawning it.
