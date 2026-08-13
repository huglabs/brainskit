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
através de `--json`, MCP, ou uma integração `--consumer human` como o visualizador
web local — recebe corpos `never-ingest`. Declarar a barreira é
obrigatório para chamadores de máquina precisamente porque o valor
irrestrito tem que ser uma escolha deliberada em vez de um padrão silencioso.
Filtragem de privacidade também se aplica a vizinhos de busca expandidos em grafo.

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
