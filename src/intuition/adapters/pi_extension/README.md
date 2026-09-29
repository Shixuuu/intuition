# Intuition — Pi agent extension

Installed by `intuition install pi` to `~/.pi/agent/extensions/intuition/`.

Loads at session start, spawns one `intuition rpc` process (JSON lines over
stdio), registers `memory_search` / `memory_read` (children) plus
`memory_note` / `memory_brief` / `memory_learn` / `memory_now` (main), builds
the stable prompt prefix once per session (and after compaction), captures
main-session turns into `raw/`, checkpoints on compaction, and runs the
Steward tick at session shutdown.

Child detection: set `CAIRN_ROLE=subagent` and `CAIRN_AGENT=<name>` in the
child process env when spawning (plan §14 Q2 fallback).
