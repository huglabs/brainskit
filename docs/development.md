# Development

The repository is uv-managed. `uv.lock` pins the development environment and
`.python-version` pins the interpreter; neither constrains an installed `bk`.

```bash
git clone https://github.com/huglabs/brainskit
cd brainskit

uv sync --group dev              # engine + pytest, ruff, mypy
uv sync --all-extras --group dev # add the Neo4j and PostgreSQL drivers

uv run pytest
uv run ruff check
uv run mypy src
```

Use `-e` while developing so `bk` always runs the working tree:

```bash
uv tool install --force -e '.[integrations]'
```

## The delivery gate

Delivery is gated by a wheel that is built, installed in a throwaway
environment, and driven through the real CLI contract, because packaged prompt
specs, output schemas and templates cannot be verified from the source tree.
The gate builds the sdist first and verifies the wheel produced *from it*,
which is the artifact publishing uploads:

```bash
./scripts/verify-wheel.sh
```

It asserts that every packaged resource shipped, then runs `init`, `capture`,
`status` and `lint` against the installed wheel. A wheel that imports but
cannot initialize a vault is a broken delivery, so the gate fails on behaviour
rather than on import.

## What CI enforces

`.github/workflows/ci.yml` runs the same gate on every push and pull request,
in a deliberate order: `ruff` and `mypy` are fast and fail on the whole tree, so
they gate the slower jobs, and the wheel job is last because it is the only one
that proves what actually ships. The suite also runs on Python 3.11, the floor
of `requires-python` — an installed `bk` has to work there, not only on the
version a maintainer happens to develop on.

`ruff format --check` is intentionally absent. This project is lint-clean, not
format-clean; adding it would fail every run on a pre-existing diff rather than
on anything a contributor did.

## Releasing

Releases are `MAJOR.MINOR.PATCH` in `[project].version`, and every published
version carries an annotated `v<version>` git tag on the commit it was built
from. PyPI rejects the re-upload of a filename it already stores, so a version
is permanent and the tag is the only durable record of which tree produced it.

Publishing runs from `.github/workflows/release.yml` on a pushed tag, through
PyPI [Trusted Publishing](https://docs.pypi.org/trusted-publishers/) — the
workflow authenticates with a short-lived OIDC token, so no API token exists to
leak or rotate. The workflow refuses to publish when the tag and
`[project].version` disagree, and runs the delivery gate before uploading
anything.

```bash
# 1. bump [project].version and add the CHANGELOG entry, then commit
git commit -am 'Release 0.5.0'
# 2. tag the exact commit the artifact will be built from
git tag -a v0.5.0 -m 'brainskit 0.5.0'
# 3. push the commit and the tag together
git push origin main --follow-tags
```

Maintainers configure the publisher once, at
[pypi.org](https://pypi.org/manage/account/publishing/), against repository
`huglabs/brainskit`, workflow `release.yml` and environment `pypi`.

To rehearse the whole path without spending a version number, publish a
pre-release (`0.5.0rc1`) — PyPI accepts it and `uv tool install brainskit` will
not select it without `--prerelease allow`.

## Conventions

Engineering conventions — and the defect classes this codebase has already paid
for — are recorded in [`AGENTS.md`](../AGENTS.md). Read it before changing the
apply gate, the privacy filter or an integration lifecycle, and record any new
defect class there.

`src/brainskit/infrastructure/codeanalysis/` is vendored third-party source, kept
byte-identical to upstream so a re-vendor is a copy rather than a merge. It is
excluded from ruff and mypy for that reason; the adapter that calls it is
checked, which is where the boundary that matters actually is. Provenance is in
[`NOTICE`](../NOTICE).
