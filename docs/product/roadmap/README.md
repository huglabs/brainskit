# Product Roadmap

This folder holds the product roadmap as plain markdown — the "now, next, later"
priorities referenced in the project CLAUDE.md documentation structure.

**Suggested layout** (pick one, don't mix):

- Horizon files: `now.md`, `next.md`, `later.md`
- Or per-initiative files: `<initiative-name>.md` with a `priority:` line at the top

**Who reads this folder:**

- `/dev` invoked with no subcommand, and any "what is next" / "what's next" /
  "o que vem agora" question, reads this folder FIRST to propose the next piece
  of work (see the "What's Next" Protocol in the root CLAUDE.md).
- `/dev plan` should write mapped priorities back here when planning produces
  roadmap-level items rather than a single work folder.

**Conventions:**

- Keep entries short: one line of intent + a link to the relevant
  `docs/product/` or `docs/work/` artifact if one exists.
- Remove or move items when they become in-flight work (`docs/work/<slug>/`).
- An empty folder means "no mapped roadmap" — `/dev` will say so and suggest
  `/dev plan` to map one.

---
<!-- doc-tracking -->
- Created: 2026-08-09 20:58
