# Security policy

## Supported versions

brainskit is pre-1.0. Security fixes land on the latest released version only;
there are no maintained back-branches.

| Version | Supported |
|---|---|
| latest release on [PyPI](https://pypi.org/project/brainskit/) | ✅ |
| anything older | ❌ |

## Reporting a vulnerability

**Please do not open a public issue.**

Report privately through GitHub:
[Security → Report a vulnerability](https://github.com/huglabs/brainskit/security/advisories/new).
That opens a draft advisory visible only to the maintainers.

Include what you need to make the report actionable: the version, the
reproduction, and what an attacker gains. A proof of concept is welcome and
never required.

You can expect an acknowledgement within a few working days, and an assessment
— fixing, not fixing, or already known, with reasoning — before any advisory is
published. Credit is given unless you ask otherwise.

## What is in scope

brainskit runs on a workstation, owns no account system and holds no
credentials, so the interesting boundaries are narrow and specific:

- **The privacy boundary.** Any path where evidence from a branch reaches a
  consumer that should not see it — including through graph expansion,
  enrichment edges, exports, persistent integrations, or a filename or branch
  name leaking in metadata after the body was redacted.
- **The write gate.** Any way to write `wiki/` without passing `bk apply`, or to
  make an installed gate report itself as enforcing while it is not.
- **Raw immutability.** Any way to alter registered evidence without `bk lint`
  detecting the change.
- **Network surfaces.** The MCP Streamable HTTP endpoint and the web viewer:
  authentication bypass, a non-loopback bind reachable without a token, Origin
  checks, or an unbounded request.
- **Secret handling.** Any path where a provider secret, DSN or token is
  persisted to disk, written into vault configuration, logged, or exposed in
  process arguments. Configuration stores only the *name* of an environment
  variable, and that is meant to be true everywhere.
- **Schema handling.** Any way a vault schema causes an outbound request;
  remote `$ref` retrieval is deliberately denied.

## What is out of scope

- The behaviour of the models you configure. brainskit gates and validates model
  output; it does not claim a model cannot produce bad content.
- Anything requiring an attacker who already has write access to the vault
  directory, `.brain/`, or the environment `bk` runs in.
- Missing hardening on a deliberately unauthenticated loopback bind that the
  operator started.
- Vulnerabilities in optional dependencies, unless brainskit's use of them is
  what makes the issue exploitable. Report those upstream; tell us too, and we
  will bump the pin.
