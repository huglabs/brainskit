# dsh-brainskit

Conecte um vault local do [Brainskit](https://github.com/huglabs/brainskit) ao
[DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) pelo cliente
MCP oficial do DSH.

O bundle inicia um processo filho `bk serve --mcp --transport stdio` junto com
o ciclo de vida do plugin DSH. O Brainskit continua responsável pela
inicialização do vault, privacidade, provedores e gravações duráveis; o DSH
descobre as ferramentas MCP no namespace `mcp__brainskit__*`.

## Pré-requisitos

- DeepSeek Harness com Node.js `^22.19.0` ou `>=24.0.0`.
- Python 3.11 ou mais recente e uma instalação fixada do Brainskit:

  ```sh
  uv tool install brainskit==0.7.0
  ```

  No Windows, use uma revisão que contenha a correção de locks portáveis do
  [#39](https://github.com/huglabs/brainskit/pull/39), ou uma versão posterior:

  ```powershell
  uv tool install --force --from git+https://github.com/huglabs/brainskit.git brainskit
  ```

- Um vault inicializado. No projeto usado pelo DSH:

  ```sh
  bk init .brainkit
  ```

O bundle nunca instala Python ou Brainskit por um script de ciclo de vida npm.

## Instalação a partir de um checkout

Até o bundle ter uma publicação npm, instale o subpacote a partir de um
checkout do Brainskit:

```sh
git clone https://github.com/huglabs/brainskit
cd brainskit
dsh plugin --profile web add ./plugins/dsh-brainskit
```

Execute `dsh web` a partir do projeto que contém `.brainkit`. O DSH inicia e
encerra o servidor stdio; não é necessária chave de API nem serviço HTTP
separado.

## Configuração

Defina as variáveis antes de iniciar o DSH:

| Variável | Padrão | Finalidade |
|---|---|---|
| `BRAINSKIT_COMMAND` | `bk` | Caminho exato do executável Brainskit; útil no Windows ou em instalações isoladas. |
| `BRAINSKIT_VAULT` | `<cwd do DSH>/.brainkit` | Vault conectado a este processo DSH. |
| `BRAINSKIT_ALLOW_MUTATIONS` | não definida | Defina como `1` para permitir mutações de wiki, arquivamento e ciclo de vida das integrações. |
| `BRAINSKIT_FAIL_ON_STARTUP_ERROR` | não definida | Defina como `1` para um executável ausente, vault inválido ou falha MCP interromper a inicialização do DSH. |

Exemplo em PowerShell:

```powershell
$env:BRAINSKIT_COMMAND = (Get-Command bk).Source
$env:BRAINSKIT_VAULT = 'C:\caminho\do\projeto\.brainkit'
dsh web
```

O processo MCP herda variáveis comuns que não parecem segredos. O cliente MCP
do DSH remove deliberadamente variáveis que parecem credenciais; se um
provedor de nuvem do Brainskit precisar de uma, passe-a explicitamente em um
override do profile em vez de gravar o segredo no YAML. Um provedor Ollama
local não precisa de chave de API.

## Autoridade padrão

O guard padrão permite recuperação, verificações de saúde, captura somente por
acréscimo e perguntas sem salvamento. Ele nega:

- `apply`, `file`, `approve` e `reject`;
- `ask` quando `save: true`;
- configuração, inicialização, encerramento e sincronização de integrações.

Defina `BRAINSKIT_ALLOW_MUTATIONS=1` somente quando o profile DSH tiver a
intenção de gerenciar essas operações. Os portões de aplicação e proveniência
do próprio Brainskit permanecem ativos de qualquer forma.

## Verificação

Após iniciar o DSH, confirme que ferramentas como `mcp__brainskit__status`,
`mcp__brainskit__search` e `mcp__brainskit__capture` aparecem. Em seguida, use
duas sessões novas:

1. Peça à sessão A para lembrar um valor único e confirme que ela chamou
   `capture`.
2. Peça à sessão B para recuperar o valor e confirme que ela chamou `search`
   ou `context` com `consumer: local`.
3. Peça ao modelo para chamar `apply`; confirme que o guard padrão nega a
   operação, a menos que o DSH tenha sido iniciado com a opção explícita de
   mutação.

## Desenvolvimento

```sh
cd plugins/dsh-brainskit
npm test
dsh plugin --profile web add .
dsh --profile web --dump-config
```

O pacote não contém script de instalação nem dependência npm de runtime. Seu
patch usa o cliente MCP distribuído com o DSH.
