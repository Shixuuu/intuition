# Changelog

## 0.1.1

- Carry the SPDX license expression and the project URLs in the published
  metadata, so PyPI shows the license and links back to the repository.
- Add `repository`, `homepage`, `bugs`, and a gallery preview image
  (`pi.image`) to the Pi package, so the listing on the Pi package gallery shows
  the screenshot and the source.
- Document the gallery listing and how to publish both artifacts.


## 0.1.0

First release.

- Markdown records in git as the source of truth, with a derived SQLite FTS5
  index you can delete and rebuild.
- Search across the vault with multi-query fusion, a weighted alias column, and
  one-hop link expansion that respects the requested record type.
- A Steward that proposes, validates, applies, and commits memory changes, with a
  path-scoped rollback so a failed run leaves the vault at HEAD and keeps the
  proposals it did not apply.
- A validator that decides trust from the cited inbox item, so external content
  cannot become a procedure, a preference, or a decision.
- A deterministic planner, so the whole system runs with no model configured.
- Raw capture with per-file consumption tracking, so turns appended after a pass
  are still observed, and retention that never deletes unread capture.
- Hermes Agent provider and a Pi package: memory tools, a prompt prefix, session
  capture, and `/intuition` for the settings and the first run.
- `intuition config` and `/intuition` share one schema for every setting.
