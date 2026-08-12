# Product Context

This directory documents the business and product context layer that helps both humans and AI understand:

- **Why** the platform exists
- **Which problems** it solves
- **How** product decisions affect engineering
- **Which compromises** and debts currently exist
- **What** is intentionally out of scope

## Directory Structure

```
product/
├── README.md                 # This file
├── AI_CONTEXT.md            # AI agent instructions
├── purpose/                 # Why does this platform exist?
│   ├── platform-purpose.md
│   ├── vision.md
│   ├── principles.md
│   └── non-goals.md
│
├── problem-space/           # Problems we solve (most important)
│   ├── README.md
│   ├── customer-problems/
│   ├── internal-problems/
│   └── assumptions.md
│
├── capabilities/            # What the platform can do
│   └── README.md
│
├── domains/                 # Business concepts
│   └── README.md
│
├── decisions/               # Product Decision Records (PDRs)
│   └── README.md
│
├── constraints/             # Business/compliance/operational limits
│   ├── business-constraints.md
│   ├── compliance-constraints.md
│   ├── operational-constraints.md
│   └── technical-constraints.md
│
├── debt/                    # Known debt by category
│   ├── README.md
│   ├── product-debt/
│   ├── technical-debt/
│   ├── design-debt/
│   ├── data-debt/
│   └── operational-debt/
│
├── roadmap/                 # Directional context
│   ├── now.md
│   ├── next.md
│   └── later.md
│
└── metrics/                 # Success metrics
    └── success-metrics.md
```

## Reading Order

For understanding the platform:

1. `purpose/platform-purpose.md` - Why we exist
2. `problem-space/README.md` - Problems we solve
3. `capabilities/README.md` - What we can do
4. `domains/` - Business concepts
5. `constraints/` - Limitations
6. `debt/README.md` - Known compromises
7. `decisions/README.md` - Product decisions

## Source of Truth

This directory is authoritative for:
- Product purpose
- Business problems
- Product constraints
- Product-level decisions (PDRs)
- Known product and technical debt

**Not stored here:**
- Implementation details → `/docs/architecture`
- Feature usage guides → `/docs/guides`
- API definitions → `/docs/api`
- Runbooks → `/docs/operations`

## Document Hierarchy

When making decisions, AI agents should prioritize:

1. Accepted decisions (PDRs)
2. Explicit constraints
3. Validated problems
4. Capabilities
5. Roadmap documents
6. Historical documents

## Causal Chain

Every document should help reconstruct:

```
Why → Problem → Outcome → Capability → Decision → Architecture → Code
```
