# dsh-brainskit

Connect a local [Brainskit](https://github.com/huglabs/brainskit) vault to
[DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) through
DSH's official MCP client.

The bundle starts one `bk serve --mcp --transport stdio` child with the DSH
plugin lifecycle. Brainskit remains responsible for vault initialization,
privacy, providers and durable writes; DSH discovers the MCP tools under the
`mcp__brainskit__*` namespace.

## Prerequisites

- DeepSeek Harness with Node.js `^22.19.0` or `>=24.0.0`.
- Python 3.11 or newer and a pinned Brainskit installation:

  ```sh
  uv tool install brainskit==0.7.0
  ```

  On Windows, use a revision containing the portable-lock fix in
  [#39](https://github.com/huglabs/brainskit/pull/39), or a later release:

  ```powershell
  uv tool install --force --from git+https://github.com/huglabs/brainskit.git brainskit
  ```

- An initialized vault. From the project DSH will use:

  ```sh
  bk init .brainkit
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

Run `dsh web` from the project that contains `.brainkit`. DSH starts and stops
the stdio server; no API key or separate HTTP service is required.

## Configuration

Set variables before launching DSH:

| Variable | Default | Purpose |
|---|---|---|
| `BRAINSKIT_COMMAND` | `bk` | Exact Brainskit executable path; useful for Windows or isolated installs. |
| `BRAINSKIT_VAULT` | `<DSH cwd>/.brainkit` | Vault connected to this DSH process. |
| `BRAINSKIT_ALLOW_MUTATIONS` | unset | Set to `1` to allow wiki, filing and integration lifecycle mutations. |
| `BRAINSKIT_FAIL_ON_STARTUP_ERROR` | unset | Set to `1` to make a missing executable, invalid vault or failed MCP handshake abort DSH startup. |

Example for PowerShell:

```powershell
$env:BRAINSKIT_COMMAND = (Get-Command bk).Source
$env:BRAINSKIT_VAULT = 'C:\path\to\project\.brainkit'
dsh web
```

The MCP child inherits ordinary non-secret environment variables. DSH's MCP
client deliberately removes credential-looking variables; if a Brainskit cloud
provider needs one, pass that variable explicitly in a profile override rather
than embedding the secret in YAML. A local Ollama provider needs no API key.

## Default authority

The default guard allows retrieval, health checks, append-only capture and
non-saving questions. It denies:

- `apply`, `file`, `approve` and `reject`;
- `ask` when `save: true`;
- integration configuration, startup, shutdown and synchronization.

Set `BRAINSKIT_ALLOW_MUTATIONS=1` only when the DSH profile is intended to
manage those operations. Brainskit's own apply and provenance gates remain
active either way.

## Verify

After DSH starts, check that tools such as `mcp__brainskit__status`,
`mcp__brainskit__search` and `mcp__brainskit__capture` appear. Then use two
fresh sessions:

1. Ask session A to remember a unique value and confirm it called `capture`.
2. Ask session B to retrieve that value and confirm it called `search` or
   `context` with `consumer: local`.
3. Ask the model to call `apply`; confirm the default guard denies it unless
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
