# Now

The field-audit remediation is **complete and closed** — see
[`04-review.md`](../../archive/2026-Q3/main-field-audit-remediation/04-review.md)
for the four-track review that closed it, and
[`01-prd.md`](../../archive/2026-Q3/main-field-audit-remediation/01-prd.md) for
the programme it reviewed. The work folder now lives under `docs/archive/2026-Q3/`.

Everything the previous `Now` carried has shipped — C5, RV1, D1, P2, D3, P1,
S2-5 and S2-4 — and none of it is on this page. `CHANGELOG.md`'s `[0.6.2]`
section is the record of what was done; this page is the record of what is open.

Ordered by how much each costs a user running the published release today.
`RV*` are findings the close-out review raised that carry no identifier from the
original audit; every other identifier is the one used in `04-review.md`. Each
row below is tracked as an issue under the
[**Now**](https://github.com/huglabs/brainskit/milestone/1) milestone — the row
carries the evidence, the issue carries the workflow.

## The guard on the next tag

`0.6.2` is out: commit `f2d863a`, tag `v0.6.2`, published to PyPI, and the
working tree clean on top of it. Everything this section was holding for a
release is now in the hands of a user who upgrades — the honest `bk status` and
`bk doctor` headlines, the `malformed` detection on `graph/graph.json` and
`views/`, lint covering `wiki/index.md` and `wiki/log.md`, the apply gate's
catalog no longer letting a page exempt itself, the pre-rename hook migration,
HTTP-status error codes, the wrapper that no longer downgrades a narrowed error,
and a `/api/status` that agrees with the CLI about `healthy`. The two
**breaking changes to an existing config format** that made it a release rather
than a checkpoint are written up under `CHANGELOG.md`'s `[0.6.2]`.

What survives the release is the guard that was supposed to protect it, and it
now applies to the tag after this one.

| ID | Intent | Where | Issue |
|----|--------|-------|-------|
| R4 | Tighten the PyPI-visibility guard **before the next tag**, because a partial upload is most expensive on the release nobody is watching for it. `release.yml:105` matches the version with a `case` glob wildcarded on both sides — `*"brainskit-$version"*` — so a `v0.6` tag is satisfied by the pre-existing `brainskit-0.6.0-py3-none-any.whl`, and the first match `exit 0`s, so a wheel-only or sdist-only upload passes. Assert both artefacts, and anchor the version at both ends | `.github/workflows/release.yml:99-116` | [#7](https://github.com/huglabs/brainskit/issues/7) |

The Trusted Publisher is registered and `v0.6.0`, `v0.6.1` and `v0.6.2` all
published cleanly — which is exactly why the wildcard has gone unnoticed for
three releases: the check has only ever been asked about complete uploads.

## Defects a user meets on `0.6.2`

Four of the fresh-user track's findings survived the whole remediation. Each was
re-derived against the working tree on 13 Aug, and the `0.6.2` batch fixed none
of them — so all four are live on the published release and on `main`, which are
now the same commit.

| ID | Intent | Where | Issue |
|----|--------|-------|-------|
| D4 | `bk code build` on a vault outside a git repository graphs the vault's own hook scripts, which `docs/code-graph.md:95-98` says are excluded. The exclusion is a path-prefix test, and the prefix is empty in exactly this case: `code_root_reason()` falls back to the vault root, `_vault_prefix()` computes `root.relative_to(root)` → `"."` → `""`, and the guard `if vault_prefix and path.startswith(...)` then excludes nothing. It fires unattended — `bk init` writes the hooks at `cli.py:2506` and bootstraps the graph four lines later | `application/codegraph.py:693-698` · `:719-721` · `infrastructure/vault.py:590-595` | [#8](https://github.com/huglabs/brainskit/issues/8) |
| D2 | `bk forget --force` is undone by the `bk reconcile` that `bk lint` tells you to run next. `forget()` deletes the registry entry and leaves the raw file on disk — that is what `--force` *means* — and `reconcile()` walks `raw/` and re-registers any hash it does not find in the registry, with a fresh `captured_at`. There is no tombstone anywhere in `src/`; the two documented steps cancel each other | `infrastructure/vault.py:488-508` · `:451-473` | [#9](https://github.com/huglabs/brainskit/issues/9) |
| D8 | `bk lint --json` emits `{"ok": true, "result": {"ok": false}}` — two answers to one question, in one document, while the process exits 1. `_emit` hardcodes the envelope's `ok` as a literal and never derives it from the payload, so this is not lint-specific: every command that fails inside a successful envelope reads the same way. `bk gate` is the one command with a bespoke envelope | `interfaces/cli.py:3714-3720` · `application/health.py:207-211` | [#10](https://github.com/huglabs/brainskit/issues/10) |
| D5 | A refusal's hint names `--code-only`, a flag no command accepts, and the design note one file above says the flag deliberately does not exist — non-code nodes are dropped by the vault "rather than trusting the caller to pass `--code-only`". A user who imports a graph of only `document` nodes is refused and handed a dead end | `application/codegraph.py:207` · `:540` · `:71-76` | [#11](https://github.com/huglabs/brainskit/issues/11) |

**Gate before moving on:** suite green · `ruff check` clean · `mypy --strict`
clean · `bk lint` clean on `docs/brain` · every negative control demonstrated
failing-then-passing, with `brainskit.__file__` asserted before the result is
trusted.

---
<!-- doc-tracking -->
- Created: 2026-08-12
- Updated: 2026-08-12 10:07
- Updated: 2026-08-13 13:15
- Updated: 2026-08-13 15:50
- Updated: 2026-08-13 17:04
- Updated: 2026-08-13 17:07
