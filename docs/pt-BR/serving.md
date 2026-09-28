*[English version](../serving.md)*

# Servindo um cofre

## Visualizador web

O visualizador é sem dependências e servido pelo motor. Seus espaços de trabalho
Graph, Sources, Wiki, Timeline e Services fornecem um canvas de grafo responsivo
com pan/zoom, busca FTS5, inspeção de fonte/página, cronologia de ingestão,
estado de integração persistente, saúde do cofre, frescor, distribuição de
branches e a fila de revisão pendente. Todas as leituras de API reutilizam
casos de uso da aplicação e seu limite de privacidade; o visualizador nunca
contorna o motor para ler arquivos do cofre.

```bash
bk --vault ./my-vault integration configure web \
  --enable --managed --host 127.0.0.1 --port 8765 --consumer human
bk --vault ./my-vault integration up web
bk --vault ./my-vault integration status web
# foreground alternative
bk --vault ./my-vault web serve
bk --vault ./my-vault integration down web
```

A URL local é `http://127.0.0.1:8765`. A vinculação além de loopback é rejeitada
a menos que `--token-env` nomeie uma variável de ambiente bearer-token preenchida.

Onze endpoints fazem leitura: `/api/health`, `/api/status`, `/api/graph`,
`/api/code-graph`, `/api/search`, `/api/proposals`, `/api/resource`,
`/api/sources`, `/api/pages`, `/api/timeline` e `/api/integrations`.

**Quatro endpoints fazem escrita**, então o visualizador não é uma superfície
somente leitura:

| Endpoint | O que escreve |
|---|---|
| `POST /api/capture` | uma fonte em `raw/`, mais o índice |
| `POST /api/ask` | uma resposta em `output/answers/` |
| `POST /api/proposals/approve` | roteia pela porta de aplicação, então `wiki/` |
| `POST /api/proposals/reject` | o estado da proposta |

O que as protege é o consumidor, não o método: toda escrita é rejeitada com
`writes_refused` a menos que o servidor tenha sido iniciado em `--consumer human`.
Um visualizador executado em `local` ou `cloud` apenas lê.

O token do portador é **opcional**, deliberadamente. Sem `--token-env`, o servidor
se vincula apenas ao loopback e é protegido apenas por verificações de Host e Origin,
o que é a troca certa para um visualizador local de um único usuário e a errada para
qualquer outra coisa. Nomear um token é o que torna possível uma vinculação não-loopback,
então as duas decisões são a mesma decisão.

## Um servidor MCP declara seu consumidor

O MCP entrega cada resposta a um modelo, e o servidor não consegue ver onde
esse modelo roda. Por isso os dois transportes respondem sob o consumidor com
que o operador os inicia:

```bash
bk --vault ./my-vault serve --mcp --transport stdio                    # cloud, o padrão
bk --vault ./my-vault serve --mcp --transport stdio --consumer local   # um agente nesta máquina
```

- **`cloud` é o padrão**, porque é a única barreira segura para encaminhar a
  qualquer lugar. `--consumer local` é o operador afirmando que o cliente roda
  nesta máquina. `human` é recusado com `policy_denied` antes que o servidor
  leia uma requisição: um modelo nunca é o leitor que `human` significa.
- **O consumidor declarado é um teto, e uma chamada só pode estreitá-lo.** Toda
  ferramenta e recurso responde sob ele. `search` e `context` continuam
  exigindo um argumento `consumer`; `cloud` sob um servidor `local` é
  respondido, e qualquer valor mais amplo que o do servidor é recusado com
  `policy_denied` (com `server_consumer` e `allowed` nos detalhes) em vez de
  respondido mais estreito em silêncio. `tools/list` oferece apenas os valores
  que o servidor responderá.
- **`capture` de um caminho de arquivo é confinado.** Texto e URLs `http(s)`
  não mudam; um arquivo só é aceito dentro do projeto do cofre (sua raiz de
  código ou um workspace registrado por `bk hooks install --root`), fora do
  próprio cofre, e nunca um arquivo de credencial como `.env*`, uma chave
  privada, ou qualquer coisa sob `~/.ssh`. `bk capture <caminho>` não é
  restringido.
- **Um servidor `cloud` não opera integrações.** `integration_configure`,
  `integration_up`, `integration_down` e `integration_sync` são recusadas com
  `policy_denied` e omitidas de `tools/list`; a recusa nomeia `bk integration
  <verbo> <nome>` e `--consumer local` como as formas de executá-las.
  `integration_status` continua disponível. Um servidor `local` executa as
  quatro, com o layout da máquina (caminhos, nomes de contêiner, nomes de
  variáveis de ambiente) removido da resposta.

Veja [a barreira de privacidade](./privacy.md#um-servidor-mcp-declara-seu-consumidor)
e o [ADR 0010](../knowledge/decisions/architecture/0010-mcp-declares-its-consumer.md).

## MCP pela rede

O transporte stdio continua sendo o padrão de rede zero. Clientes de rede usam o
endpoint HTTP MCP Streamable sem estado em `/mcp`; cada requisição requer o mesmo
token Bearer pré-compartilhado, carregado apenas da variável de ambiente explicitamente
nomeada.

```bash
export BRAINKIT_MCP_TOKEN='use-a-secret-manager-in-production'
bk --vault ./my-vault serve --mcp --transport http \
  --host 127.0.0.1 --port 8766 --token-env BRAINKIT_MCP_TOKEN
```

O transporte HTTP recebe o mesmo `--consumer`, com o mesmo padrão `cloud`.

Uma requisição de inicialização direta se parece com isto:

```bash
curl http://127.0.0.1:8766/mcp \
  -H "Authorization: Bearer $BRAINKIT_MCP_TOKEN" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  --data '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"example","version":"1"}}}'
```

Requisições subsequentes devem incluir `MCP-Protocol-Version: 2025-06-18`. Origens
do navegador são verificadas contra valores `--allowed-origin` repetíveis, corpos
de requisição são limitados, e respostas desabilitam o cache. Uma vinculação
não-loopback também requer `--tls-cert` e `--tls-key`; HTTP plano é permitido
apenas em loopback. Isto é intencionalmente autenticação de token pré-compartilhado
para agentes confiáveis, não uma implementação de servidor de autorização OAuth.
O servidor retorna cada resposta POST como JSON e funciona sem sessões; fluxos
SSE `GET` autônomos não estão habilitados.

Ambos os transportes expõem as mesmas ferramentas, apoiadas pelos mesmos casos
de uso da aplicação que o CLI:

| Grupo | Ferramentas |
|---|---|
| Evidência | `capture`, `search`, `context`, `file` |
| Compilação | `apply`, `ask`, `resurface` |
| Revisão | `proposals`, `approve`, `reject` |
| Operações | `status`, `lint` |
| Integrações | `integration_configure`, `integration_status`, `integration_up`, `integration_down`, `integration_sync` |

Um servidor `cloud` lista e responde todas as linhas exceto as quatro
ferramentas de ciclo de vida de integração; apenas `integration_status` resta
nesse grupo.

`bk vaults` está deliberadamente ausente daquela lista: um servidor MCP é iniciado
para um cofre e responde sob o limite declarado desse cofre, então uma ferramenta
que alcançasse cofres não relacionados alargaria o limite concedido ao chamador.

---
<!-- doc-tracking -->
- Created: 2026-08-13 09:33
