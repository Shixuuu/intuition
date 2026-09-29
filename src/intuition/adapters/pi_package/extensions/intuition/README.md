# Intuition — Pi package

Installed by `intuition install pi`, which copies this package to
`~/.pi/agent/intuition/`, writes the tool manifest
`extensions/intuition/tools.json` from the package's single schema source, and
registers the package with Pi (`pi install <dir>`), so `pi list` shows it.

The extension spawns one `intuition rpc` process per session (JSON lines over
stdio), registers the memory tools it finds in that manifest, builds the stable
prompt prefix once per session and again after compaction, captures main-session
turns into `raw/`, and asks the Steward for a session-end pass.

Tool surface: `memory_search` and `memory_read` for children, plus
`memory_note`, `memory_forget`, `memory_now`, `memory_brief`, `memory_learn`,
`memory_timeline`, `memory_secure_get` and `memory_status` for the main session.

Child detection: set `INTUITION_ROLE=subagent` and `INTUITION_AGENT=<name>` in
the child process env when spawning it.
