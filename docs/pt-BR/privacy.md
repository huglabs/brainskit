*[English version](../privacy.md)*

# A barreira de privacidade

Chamadores de máquina devem declarar sua barreira de privacidade:

```bash
bk --vault ./my-vault context "topic" --consumer cloud --json
bk --vault ./my-vault search "topic" --consumer local --json
```

`cloud` recebe apenas evidência elegível para cloud e `local` exclui
`never-ingest`. `human` não aplica restrição nenhuma: é o padrão para
uso interativo não-JSON, e um chamador de máquina que o nomeia explicitamente —
através de `--json`, ou uma integração `--consumer human` como o visualizador
web local — recebe corpos `never-ingest`. O MCP nunca serve `human` (abaixo).
Declarar a barreira é obrigatório para chamadores de máquina precisamente
porque o valor irrestrito tem que ser uma escolha deliberada em vez de um
padrão silencioso. Filtragem de privacidade também se aplica a vizinhos de
busca expandidos em grafo.

## Um servidor MCP declara seu consumidor

O MCP entrega suas respostas a um modelo, e o servidor não consegue ver onde
esse modelo roda. Então o operador o declara ao iniciar o servidor:

```bash
bk --vault ./my-vault serve --mcp --transport stdio                    # cloud
bk --vault ./my-vault serve --mcp --transport stdio --consumer local   # um agente nesta máquina
```

`cloud` é o padrão e `human` é recusado. O consumidor declarado é um teto em
toda ferramenta e recurso: `status` é o relatório filtrado de `/api/status`
(sem o caminho do cofre em `cloud`), `proposals`, `lint` e os recursos
descartam o que o teto não pode ver, e `ask`, `resurface` e `lint --semantic`
não leem mais amplo que ele mesmo quando um modelo local está mapeado. `search`
e `context` continuam exigindo um argumento `consumer`, e ele só pode estreitar
o do servidor; um mais amplo é recusado com `policy_denied` em vez de
respondido mais estreito em silêncio. `file`, `approve` e `reject` não alcançam
uma fonte ou proposta fora do teto — mover uma fonte é como sua privacidade
muda. Um servidor `cloud` não executa `integration_configure`,
`integration_up`, `integration_down` nem `integration_sync`: são ações do
operador nesta máquina, então são recusadas com `policy_denied`, nomeando `bk
integration <verbo>` e `--consumer local` em vez de qualquer caminho, e ficam
fora de `tools/list`. `integration_status` continua disponível. Veja o
[ADR 0010](../knowledge/decisions/architecture/0010-mcp-declares-its-consumer.md).

Via MCP, `capture` aceita texto e URLs `http(s)` como sempre, mas um caminho de
arquivo apenas dentro do projeto do cofre (sua raiz de código, ou um workspace
registrado por `bk hooks install --root`), fora do próprio cofre, e nunca um
arquivo de credencial: `.env*` (exceto modelos como `.env.example`), `*.pem`,
`*.key`, chaves privadas SSH, `.netrc`, `.npmrc`, `.pypirc`, qualquer coisa sob
`~/.ssh`, `~/.aws`, `~/.config/gcloud` ou um diretório `.git`. `bk capture
<caminho>` é o próprio comando do operador e não é restringido.

## Filtragem executa após expansão, não antes

Filtrar os acertos diretos primeiro permitiria que um link de saída ou um
backlink puxasse um nó restrito de volta para a vista através de seu vizinho.
Assim, o filtro executa no grafo terminado, uma vez que cada nó e aresta existe.

## Um nome de arquivo é divulgação

Filtragem cobre corpos de nó e metadados de nó igualmente. Um nome de arquivo
e sua branch são eles próprios divulgação, então uma fonte redatada não contribui
nem uma. Pela mesma razão `search` e `context` reportam `redacted` como
uma contagem e nunca descrevem o que foi retido: o bundle que `context`
retorna é o payload entregue a um modelo cloud, e nomear uma fonte retida
ali derrotaria a barreira que a eliminou.

## Julgamento herda a política mais rigorosa

Quando evidência abrange branches, o roteador de julgamento aplica a política
mais rigorosa no conjunto: `never-ingest` nega a chamada, `local-only` requer
Ollama, e roteamento cloud é permitido apenas quando cada branch contribuinte
o permite. Arestas de enriquecimento seguem a mesma regra através da mesma
função — veja [Enrichment](./enrichment.md).

## Cada saída a carrega

Exportações e integrações persistentes são governadas pela mesma barreira, e
alvos de arquivo padrão para `local` então uma exportação nunca emite evidência
`never-ingest` a menos que `human` seja nomeado deliberadamente. Veja
[Egress carries the boundary](./integrations.md#egress-carries-the-boundary).

---
<!-- doc-tracking -->
- Created: 2026-08-13 09:32
