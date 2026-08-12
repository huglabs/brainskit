---
id: "synthesis:brainkit-vault-audit"
type: "synthesis"
title: "Brainkit Vault Audit (2026-08-11)"
aliases:
sources:
  - "0340d6a392c389983e750906f4d1836708062737055ab568a1aaa33063bc6b47"
updated_at: "2026-08-11T20:20:54.106606+00:00"
---

# Brainkit Vault Audit (2026-08-11)

Audit of the brainkit codebase performed with bk itself. Key findings: (1) the code graph is mostly non-owned nodes (vendored codeanalysis ~571, web template bundle ~1476 incl. three.min.js at 345 edges, tests ~1417) leaving ~864 owned-source nodes; fix by ignoring both vendored dirs or scoping the build. (2) Owned coupling hubs: ValidationError 266, BrainkitService 254, FileVault 226, cli.py 157, SqliteFtsIndex 156, MarkdownGraph 144; cli.py is the largest owned file at 3337 lines. (3) Zero import cycles. (4) The vault stub was broken (leftover .brain/code-cache, no config) and required force-init. (5) Enforcement layers uninstalled. (6) qwen2.5:3b gives generic ask answers; a Qwen3.6-14B local model would serve query far better. (7) 16/29 grammars missing but Python covered. [^source:0340d6a392c389983e750906f4d1836708062737055ab568a1aaa33063bc6b47]
