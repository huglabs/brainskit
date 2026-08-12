Vault audit of the brainkit codebase using bk itself (2026-08-11).

FINDING 1 - Graph pollution: the code graph has 4,381 nodes but only ~864 (20%) are owned source. The rest is the vendored Graphify codeanalysis/ tree (571), the web template bundle templates/web (1,476, of which three.min.js is the single most-connected node in the whole graph at 345 edges) and tests (1,417). The config `ignore` list and .gitignore cover neither src/brainkit/templates/web/ nor src/brainkit/infrastructure/codeanalysis/, so hubs/communities/affected answers are dominated by noise. Fix: add both dirs to the vault ignore list and/or scope `bk code build` PATHs to owned source; consider a `--scope owned` flag.

FINDING 2 - Owned-source coupling hubs: after removing noise, the real cores are ValidationError (266 edges), BrainkitService (254), FileVault (226), cli.py (157), SqliteFtsIndex (156), MarkdownGraph (144). cli.py is the largest owned file (3,337 lines) and the single biggest interface-layer god module.

FINDING 3 - Zero import cycles: the vendored + owned code has 0 import cycles. Healthy.

FINDING 4 - Vault stub was broken: docs/brain existed with leftover .brain/code-cache but no config.json; needed --force init. Nested-vault commit_lint reports inactive.

FINDING 5 - Enforcement layers: write_gate/session_status/commit_lint/instructions all unininstalled in this vault.

FINDING 6 - bk ask quality: with qwen2.5:3b configured for query, answers are generic. A Qwen3.6-14B-A3B model is available on this host's ollama and would serve query/ask far better.

FINDING 7 - Doctor: 16/29 tree-sitter grammars missing but Python (the repo's language) is covered.