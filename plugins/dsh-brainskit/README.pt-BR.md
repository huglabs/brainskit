# dsh-brainskit

Conecte um vault local do [Brainskit](https://github.com/huglabs/brainskit) ao
[DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) pelo cliente
MCP oficial do DSH.

O bundle inicia um processo filho `bk serve --mcp --transport stdio --consumer
cloud` junto com o ciclo de vida do plugin DSH. O Brainskit continua responsável pela
inicialização do vault, privacidade, provedores e gravações duráveis; o DSH
descobre as ferramentas MCP no namespace `mcp__brainskit__*`.

## Pré-requisitos

- DeepSeek Harness com Node.js `^22.19.0` ou `>=24.0.0`.
- Python 3.11 ou mais recente e uma instalação fixada do Brainskit. O bundle
  exige a 0.8.0 ou posterior: ele passa `bk serve --consumer`, que versões
  anteriores não aceitam, e a 0.8.0 também traz a correção de locks portáveis
  para Windows ([#39](https://github.com/huglabs/brainskit/pull/39)):

  ```sh
  uv tool install brainskit==0.8.0
  ```

  Enquanto a 0.8.0 não estiver no PyPI, instale a partir do repositório:

  ```sh
  uv tool install --force --from git+https://github.com/huglabs/brainskit.git brainskit
  ```

- Um vault inicializado. No projeto usado pelo DSH:

  ```sh
  bk init .brainskit
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

Execute `dsh web` a partir do projeto que contém `.brainskit`. O DSH inicia e
encerra o servidor stdio; não é necessária chave de API nem serviço HTTP
separado.

## Configuração

Defina as variáveis antes de iniciar o DSH:

| Variável | Padrão | Finalidade |
|---|---|---|
| `BRAINSKIT_COMMAND` | `bk` | Caminho exato do executável Brainskit; útil no Windows ou em instalações isoladas. |
| `BRAINSKIT_VAULT` | `<cwd do DSH>/.brainskit` | Vault conectado a este processo DSH. |
| `BRAINSKIT_CONSUMER` | `cloud` | Teto de privacidade com que o servidor é iniciado. Use `local` somente quando o DSH roda um modelo nesta máquina; veja [Privacidade](#privacidade). |
| `BRAINSKIT_ALLOW_MUTATIONS` | não definida | Defina como `1` para permitir mutações de wiki e arquivamento, e do ciclo de vida das integrações quando `BRAINSKIT_CONSUMER=local`; veja [Autoridade padrão](#autoridade-padrão). |
| `BRAINSKIT_FAIL_ON_STARTUP_ERROR` | não definida | Defina como `1` para um executável ausente, vault inválido ou falha MCP interromper a inicialização do DSH. |

Exemplo em PowerShell:

```powershell
$env:BRAINSKIT_COMMAND = (Get-Command bk).Source
$env:BRAINSKIT_VAULT = 'C:\caminho\do\projeto\.brainskit'
dsh web
```

> [!WARNING]
> Mantenha o servidor MCP com o nome `brainskit`. O guard reconhece as
> ferramentas do Brainskit apenas pelo prefixo `mcp__brainskit__` que o DSH
> deriva de `serverName`; uma chamada de ferramenta no DSH não carrega nenhum
> outro indício de qual servidor a registrou, então o guard não consegue
> perceber uma renomeação. Se um override do profile renomear o servidor, o
> guard não reconhece nada e todas as ferramentas do Brainskit, inclusive
> `apply`, rodam sem proteção.

O processo MCP herda variáveis comuns que não parecem segredos. O cliente MCP
do DSH remove deliberadamente variáveis que parecem credenciais; se um
provedor de nuvem do Brainskit precisar de uma, passe-a explicitamente em um
override do profile em vez de gravar o segredo no YAML. Um provedor Ollama
local não precisa de chave de API.

## Privacidade

O servidor é iniciado com `--consumer cloud`, e essa declaração é o seu teto:
toda ferramenta responde sob ela, e um `consumer` por chamada só pode
estreitá-la. O modelo padrão do DSH é a API em nuvem da DeepSeek, então todo
resultado lido pelo modelo sai da máquina, e uma leitura `local` enviaria
branches restritos à máquina local a um terceiro.

Evidências em branches `local-only`, inclusive um inbox inicializado para
Ollama, ficam invisíveis ao modelo nesse padrão. Isso é intencional.

Defina `BRAINSKIT_CONSUMER=local` somente quando o DSH estiver configurado com
um modelo que roda nesta máquina. Qualquer outro valor, inclusive `human`, faz
o servidor recusar a inicialização e o guard negar toda chamada ao Brainskit.

O guard aplica o mesmo teto antes de a chamada chegar ao servidor. Sob `cloud`,
ele nega qualquer chamada que passe `consumer: local` ou `consumer: human`; sob
`local`, permite `local` e `cloud` e nega `human`. O modelo é orientado a omitir
`consumer` ou passar o valor declarado.

## Autoridade padrão

O guard padrão é uma lista de permissão. Ele permite `search`, `context`,
`capture`, `ask` sem salvamento, `status`, `lint`, `proposals` e
`integration_status`, e nega todas as outras ferramentas do Brainskit,
incluindo:

- `apply`, `file`, `approve` e `reject`;
- `ask` com qualquer valor de `save` diferente de `false` (o Brainskit lê o
  valor como verdade em Python, então `"false"` salvaria);
- `resurface`, que escreve em `output/resurface/` e uma anotação de freshness;
- configuração, inicialização, encerramento e sincronização de integrações. O
  próprio servidor as recusa sob `cloud` (`policy_denied`) e as omite da sua
  lista de ferramentas, então elas só rodam com `BRAINSKIT_CONSUMER=local` e a
  liberação abaixo; fora isso, use `bk integration <verbo>` em um terminal;
- qualquer ferramenta que uma versão futura do Brainskit adicionar, até que
  esta lista a nomeie.

Defina `BRAINSKIT_ALLOW_MUTATIONS=1` somente quando o profile DSH tiver a
intenção de gerenciar essas operações. Os portões de aplicação e proveniência
do próprio Brainskit permanecem ativos de qualquer forma.

Independentemente dessa liberação, qualquer chamada cujo `consumer` seja mais
amplo que o declarado é negada (veja [Privacidade](#privacidade)). Todo
resultado passa pelo modelo, então `human` nunca é a fronteira correta para
esta ponte.

`capture` continua na lista de permissão padrão. Ele aceita texto e URLs, e um
caminho de arquivo somente quando o arquivo está dentro do projeto e não é um
segredo como `.env`; o servidor recusa todo o resto.

## Verificação

Após iniciar o DSH, confirme que ferramentas como `mcp__brainskit__status`,
`mcp__brainskit__search` e `mcp__brainskit__capture` aparecem. Em seguida, use
duas sessões novas:

1. Peça à sessão A para lembrar um valor único e confirme que ela chamou
   `capture`.
2. Peça à sessão B para recuperar o valor e confirme que ela chamou `search`
   ou `context` com `consumer: cloud` ou sem `consumer`. Uma captura entra no
   inbox do vault, então o valor só volta quando `inbox_policy` é `cloud`, que
   é o que o `bk init` grava ao escolher um provedor em nuvem. Um vault
   configurado para Ollama mantém o inbox `local-only`, e uma leitura `cloud`
   sem resultado ali é a fronteira funcionando, não uma falha.
3. Peça ao modelo para chamar `search` com `consumer: local`; confirme que o
   guard nega a chamada por ser mais ampla que o `cloud` declarado.
4. Peça ao modelo para chamar `apply`; confirme que o guard padrão nega a
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
