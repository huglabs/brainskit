# Knowledge Base

Organized by **knowledge type** for optimal retrieval and learning.

## Structure

```
docs/knowledge/
├── decisions/           # Things we decided
│   ├── architecture/    # System design decisions (ADRs)
│   ├── technology/      # Technology choices
│   └── process/         # Workflow decisions
│
├── patterns/            # Reusable engineering patterns
│
├── domain/              # Domain models, bounded contexts, glossary.md
│
└── migrations/          # Things we transformed
    ├── legacy/          # Legacy analysis
    └── transformations/ # Migration logs
```

Two things that used to live here now live elsewhere:

- **Discoveries** (research, incidents, experiments) → the brainskit vault at `docs/brain/`. Evidence enters with `bk capture` + `bk file --to <branch>`; pages are written only by `bk apply`. Direct writes under `docs/brain/{raw,wiki}/` are refused by a PreToolUse hook. See the `brainskit` skill.
- **Deliverables** (PRDs, specs, reviews, diagrams) are per-feature now → the feature's work folder at `docs/work/<slug>/` (`01-prd.md`, `02-spec.md`, `04-review.md`, `03-diagrams/`), where `<slug>` = `CU-<clickup-id>-<short-name>`.

## Usage by Workflow Phase

| Phase | Skill | Output Location |
|-------|-------|-----------------|
| Discovery | `/huglabs/grill-me` | `bk capture` → `docs/brain/raw/40-research/` |
| Planning | `/huglabs/to-prd` | `docs/work/<slug>/01-prd.md` |
| Planning | `/huglabs/to-issues` | `docs/work/<slug>/02-spec.md` |
| Implementation | `/huglabs/tdd` | (code, not docs) |
| Review | `/huglabs/improve-architecture` | `decisions/architecture/` |

## Naming Conventions

### Decisions (ADRs)
```
ADR-{NNN}-{short-description}.md
Example: ADR-001-chose-hexagonal-architecture.md
```

### Research
```
{YYYY-MM-DD}-{topic}.md
Example: 2026-06-21-websocket-patterns.md
```

### PRDs
```
{NNN}-{feature-name}-prd.md
Example: 001-notifications-prd.md
```

### Reviews
```
{NNN}-{feature-name}-review.md
Example: 001-notifications-review.md
```

## Integration with Learning System

This knowledge base feeds into the adaptive skill composition system:

1. **Decisions** → Inform future architectural choices
2. **Discoveries** (in the vault) → Become skill context for similar tasks
3. **Domain** → Defines domain vocabulary for grilling
4. **Patterns** → Reused by architect skills

## Search Tips

```bash
# Find all architecture decisions
ls docs/knowledge/decisions/architecture/

# Find research on a topic
bk --vault docs/brain search "websocket" --consumer local --json

# Find patterns for a domain
grep -r "notification" docs/knowledge/patterns/
```
