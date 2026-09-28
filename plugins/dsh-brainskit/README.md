# dsh-brainskit

Connect a local [Brainskit](https://github.com/huglabs/brainskit) vault to
[DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) through
DSH's official MCP client.

The bundle starts one `bk serve --mcp --transport stdio --consumer cloud`
child with the DSH plugin lifecycle. Brainskit remains responsible for vault
initialization, privacy, providers and durable writes; DSH discovers the MCP
tools under the `mcp__brainskit__*` namespace.

## Prerequisites

- DeepSeek Harness with Node.js `^22.19.0` or `>=24.0.0`.
- Python 3.11 or newer and a pinned Brainskit installation. The bundle needs
  0.8.0 or later: it passes `bk serve --consumer`, which earlier releases do
  not accept, and 0.8.0 also carries the Windows portable-lock fix
  ([#39](https://github.com/huglabs/brainskit/pull/39)):

  ```sh
  uv tool install brainskit==0.8.0
  ```

  Until 0.8.0 is on PyPI, install from the repository instead:

  ```sh
  uv tool install --force --from git+https://github.com/huglabs/brainskit.git brainskit
  ```

- An initialized vault. From the project DSH will use:

  ```sh
  bk init .brainskit
  ```

The bundle never installs Python or Brainskit from an npm lifecycle script.

## Install from a checkout

Until the bundle has an npm release, install its subpackage from a Brainskit
checkout:

```sh
git clone https://github.com/huglabs/brainskit
cd brainskit
dsh plugin --profile web add ./plugins/dsh-brainskit
```

Run `dsh web` from the project that contains `.brainskit`. DSH starts and stops
the stdio server; no API key or separate HTTP service is required.

## Configuration

Set variables before launching DSH:

| Variable | Default | Purpose |
|---|---|---|
| `BRAINSKIT_COMMAND` | `bk` | Exact Brainskit executable path; useful for Windows or isolated installs. |
| `BRAINSKIT_VAULT` | `<DSH cwd>/.brainskit` | Vault connected to this DSH process. |
| `BRAINSKIT_CONSUMER` | `cloud` | Privacy ceiling the server is started with. Set to `local` only when DSH runs a model on this machine; see [Privacy](#privacy). |
| `BRAINSKIT_ALLOW_MUTATIONS` | unset | Set to `1` to allow wiki and filing mutations, and integration lifecycle mutations when `BRAINSKIT_CONSUMER=local`; see [Default authority](#default-authority). |
| `BRAINSKIT_FAIL_ON_STARTUP_ERROR` | unset | Set to `1` to make a missing executable, invalid vault or failed MCP handshake abort DSH startup. |

Example for PowerShell:

```powershell
$env:BRAINSKIT_COMMAND = (Get-Command bk).Source
$env:BRAINSKIT_VAULT = 'C:\path\to\project\.brainskit'
dsh web
```

> [!WARNING]
> Keep the MCP server named `brainskit`. The guard recognises Brainskit tools
> only by the `mcp__brainskit__` prefix DSH derives from `serverName`; a DSH
> tool call carries no other trace of which server registered it, so the guard
> cannot notice a rename. If a profile override renames the server, the guard
> matches nothing and every Brainskit tool, `apply` included, runs unguarded.

The MCP child inherits ordinary non-secret environment variables. DSH's MCP
client deliberately removes credential-looking variables; if a Brainskit cloud
provider needs one, pass that variable explicitly in a profile override rather
than embedding the secret in YAML. A local Ollama provider needs no API key.

## Privacy

The server is started with `--consumer cloud`, and that declaration is its
ceiling: every tool answers under it, and a per-call `consumer` can only
narrow it. DSH's default model is DeepSeek's cloud API, so every result the
model reads leaves the machine, and a `local` read would send local-only
branches to a third party.

Evidence in `local-only` branches, including an inbox initialized for
Ollama, is invisible to the model under this default. That is intended.

Set `BRAINSKIT_CONSUMER=local` only when DSH is configured with a model that
runs on this machine. Any other value, including `human`, makes the server
refuse to start and the guard deny every Brainskit call.

The guard enforces the same ceiling before a call reaches the server. Under
`cloud` it denies any call that passes `consumer: local` or `consumer: human`;
under `local` it permits `local` and `cloud` and denies `human`. The model is
told to omit `consumer` or pass the declared value.

## Default authority

The default guard is an allow-list. It permits `search`, `context`, `capture`,
non-saving `ask`, `status`, `lint`, `proposals` and `integration_status`, and
denies every other Brainskit tool, including:

- `apply`, `file`, `approve` and `reject`;
- `ask` with any `save` value other than `false` (Brainskit reads it as a
  Python truth value, so `"false"` would save);
- `resurface`, which writes `output/resurface/` and a freshness annotation;
- integration configuration, startup, shutdown and synchronization. The
  server refuses these itself under `cloud` (`policy_denied`) and leaves them
  out of its tool list, so they run only with `BRAINSKIT_CONSUMER=local` and
  the opt-in below; otherwise use `bk integration <verb>` in a terminal;
- any tool a later Brainskit release adds, until this list names it.

Set `BRAINSKIT_ALLOW_MUTATIONS=1` only when the DSH profile is intended to
manage those operations. Brainskit's own apply and provenance gates remain
active either way.

Independently of that opt-in, any call whose `consumer` is wider than the
declared one is denied (see [Privacy](#privacy)). Every result is relayed
through the model, so `human` is never the right boundary for this bridge.

`capture` stays in the default allow-list. It accepts text and URLs, and a
file path only when the file is inside the project and is not a secret such as
`.env`; the server refuses anything else.

## Verify

After DSH starts, check that tools such as `mcp__brainskit__status`,
`mcp__brainskit__search` and `mcp__brainskit__capture` appear. Then use two
fresh sessions:

1. Ask session A to remember a unique value and confirm it called `capture`.
2. Ask session B to retrieve that value and confirm it called `search` or
   `context` with `consumer: cloud` or no `consumer` at all. A capture lands
   in the vault's inbox, so the value comes back only when `inbox_policy` is
   `cloud`, which is what `bk init` writes when you choose a cloud provider.
   A vault set up for Ollama keeps its inbox `local-only`, and a `cloud` read
   returning nothing there is the boundary working, not a failure.
3. Ask the model to call `search` with `consumer: local`; confirm the guard
   denies it as wider than the declared `cloud`.
4. Ask the model to call `apply`; confirm the default guard denies it unless
   DSH was launched with the explicit mutation opt-in.

## Development

```sh
cd plugins/dsh-brainskit
npm test
dsh plugin --profile web add .
dsh --profile web --dump-config
```

The package contains no install script and no runtime npm dependency. Its patch
uses the MCP client shipped with DSH.
