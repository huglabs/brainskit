# Now

The field-audit remediation is **complete and closed** — see
[`04-review.md`](../../archive/2026-Q3/main-field-audit-remediation/04-review.md)
for the four-track review that closed it, and
[`01-prd.md`](../../archive/2026-Q3/main-field-audit-remediation/01-prd.md) for
the programme it reviewed. The work folder now lives under `docs/archive/2026-Q3/`.

Everything the previous `Now` carried has shipped — C5, RV1, D1, P2, D3, P1,
S2-5 and S2-4 — and none of it is on this page. `CHANGELOG.md`'s `[Unreleased]`
section is the record of what was done; this page is the record of what is open.

Ordered by how much each costs a user running the published release today.
`RV*` are findings the close-out review raised that carry no identifier from the
original audit; every other identifier is the one used in `04-review.md`.

## Ship what is already fixed

`0.6.1` is published and tagged. **25 paths are uncommitted on top of it**, and
a user on `0.6.1` has none of them: the honest `bk status` and `bk doctor`
headlines, the `malformed` detection on `graph/graph.json` and `views/`, lint
covering `wiki/index.md` and `wiki/log.md`, the apply gate's catalog no longer
letting a page exempt itself, the pre-rename hook migration, HTTP-status error
codes, the wrapper that no longer downgrades a narrowed error, and a
`/api/status` that agrees with the CLI about `healthy`.

Two of those are **breaking changes to an existing config format**, which is
what makes this a release rather than a checkpoint.

| ID | Intent | Where |
|----|--------|-------|
| C6 | Cut **`0.6.2`**. Both config changes are behaviour changes to a file the user already has: a relative `sources` entry now resolves against the vault root, and the Obsidian integration's `path` does too — and that one *writes*. A vault that was capturing files may capture none; a sync that was landing in one directory will land in another. Neither is opt-in | `CHANGELOG.md` `[Unreleased]` · `.github/workflows/release.yml` |
| R4 | Tighten the PyPI-visibility guard **before** tagging, because this is the release where a partial upload is most expensive. `release.yml:105` matches the version with a `case` glob wildcarded on both sides — `*"brainskit-$version"*` — so a `v0.6` tag is satisfied by the pre-existing `brainskit-0.6.0-py3-none-any.whl`, and the first match `exit 0`s, so a wheel-only or sdist-only upload passes. Assert both artefacts, and anchor the version at both ends | `.github/workflows/release.yml:99-116` |

The Trusted Publisher is registered and `v0.6.0` and `v0.6.1` both published
cleanly, so this is a tag push rather than a spike.

## Defects a user meets on `0.6.1` and still on `main`

Four of the fresh-user track's findings survived the whole remediation. Each was
re-derived against the working tree on 13 Aug; none is fixed by the batch above.

| ID | Intent | Where |
|----|--------|-------|
| D4 | `bk code build` on a vault outside a git repository graphs the vault's own hook scripts, which `docs/code-graph.md:95-98` says are excluded. The exclusion is a path-prefix test, and the prefix is empty in exactly this case: `code_root_reason()` falls back to the vault root, `_vault_prefix()` computes `root.relative_to(root)` → `"."` → `""`, and the guard `if vault_prefix and path.startswith(...)` then excludes nothing. It fires unattended — `bk init` writes the hooks at `cli.py:2506` and bootstraps the graph four lines later | `application/codegraph.py:693-698` · `:719-721` · `infrastructure/vault.py:590-595` |
| D2 | `bk forget --force` is undone by the `bk reconcile` that `bk lint` tells you to run next. `forget()` deletes the registry entry and leaves the raw file on disk — that is what `--force` *means* — and `reconcile()` walks `raw/` and re-registers any hash it does not find in the registry, with a fresh `captured_at`. There is no tombstone anywhere in `src/`; the two documented steps cancel each other | `infrastructure/vault.py:488-508` · `:451-473` |
| D8 | `bk lint --json` emits `{"ok": true, "result": {"ok": false}}` — two answers to one question, in one document, while the process exits 1. `_emit` hardcodes the envelope's `ok` as a literal and never derives it from the payload, so this is not lint-specific: every command that fails inside a successful envelope reads the same way. `bk gate` is the one command with a bespoke envelope | `interfaces/cli.py:3714-3720` · `application/health.py:207-211` |
| D5 | A refusal's hint names `--code-only`, a flag no command accepts, and the design note one file above says the flag deliberately does not exist — non-code nodes are dropped by the vault "rather than trusting the caller to pass `--code-only`". A user who imports a graph of only `document` nodes is refused and handed a dead end | `application/codegraph.py:207` · `:540` · `:71-76` |

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
