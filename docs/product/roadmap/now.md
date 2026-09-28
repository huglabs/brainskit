# Now

The field-audit remediation is **complete and closed** — see
[`04-review.md`](../../archive/2026-Q3/main-field-audit-remediation/04-review.md)
for the four-track review that closed it, and
[`01-prd.md`](../../archive/2026-Q3/main-field-audit-remediation/01-prd.md) for
the programme it reviewed. The work folder now lives under `docs/archive/2026-Q3/`.

Everything the previous `Now` carried has shipped — C5, RV1, D1, P2, D3, P1,
S2-5 and S2-4 — and none of it is on this page. `CHANGELOG.md`'s `[0.6.2]`
section is the record of what was done; this page is the record of what is open.

## Fixed for 0.8.0, pending the release

Every row this page carried after `0.6.2` is fixed on branch `release-0.8.0`
and waits only for the `0.8.0` tag, so none of it is listed here any more —
`CHANGELOG.md`'s `[0.8.0]` section is the record of what was done:

- **R4** ([#7](https://github.com/huglabs/brainskit/issues/7)) — the
  PyPI-visibility guard requires the wheel and the sdist for exactly the tagged
  version, anchored, and a local version (`+…`) is refused before anything
  is installed. It guards the `0.8.0` tag itself.
- **D4** ([#8](https://github.com/huglabs/brainskit/issues/8)) — `bk code build`
  no longer graphs the vault's own files or its installed hook scripts when the
  vault is the code root.
- **D2** ([#9](https://github.com/huglabs/brainskit/issues/9)) — `bk forget`
  leaves a tombstone that `bk reconcile` and `bk watch` respect.
- **D8** ([#10](https://github.com/huglabs/brainskit/issues/10)) — the `--json`
  envelope's `ok`, the exit status and MCP `isError` are read from one rule.
- **D5** ([#11](https://github.com/huglabs/brainskit/issues/11)) — no refusal
  names the nonexistent `--code-only` any more.

Until `0.8.0` is published, all five are still live for a user on the
published release, which is why their issues under the
[**Now**](https://github.com/huglabs/brainskit/milestone/1) milestone stay
open until the tag.

---
<!-- doc-tracking -->
- Created: 2026-08-12
- Updated: 2026-08-12 10:07
- Updated: 2026-08-13 13:15
- Updated: 2026-08-13 15:50
- Updated: 2026-08-13 17:04
- Updated: 2026-08-13 17:07
- Updated: 2026-09-27
