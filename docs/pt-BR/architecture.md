*[English version](../architecture.md)*

# Arquitetura

```text
interfaces (CLI, MCP, web API/viewer — reads always, writes only at --consumer human)
        ↓
application (use cases and ports)
        ↓
domain (entities, values, policies, invariants)
        ↑
infrastructure (vault, FTS5, LLM and persistent integration adapters)
```

O domain não tem dependência do CLI, filesystem, SQLite ou de um fornecedor de LLM.

## Os módulos da application

Dentro da camada application, `BrainskitService` é uma fachada que não possui nada:
ela compõe os colaboradores abaixo e delega. Suas importações formam um DAG —
cada uma depende apenas das que estão acima dela — então qualquer uma pode ser lida, testada ou
substituída sem carregar o resto.

| Module | Owns |
|---|---|
| `pages` | O formato de documento de página: renderizar, analisar e os auxiliares de texto derivados dele |
| `privacy` | A única resposta para "este consumidor pode ver isto?" |
| `freshness` | Estado da página aplicada e impressões digitais de artefatos derivados |
| `judgment` | O loop de reparo delimitado que cada trabalho vinculado a esquema compartilha |
| `compilation` | O portão apply — o único caminho que escreve em `wiki/` |
| `retrieval` | Busca BM25 e pacote de evidências delimitado, filtrado após expansão |
| `health` | Lint estrutural, `status` e o relatório de projeção |
| `filing` | Propor uma branch, depois aguardar ou executar conforme a política de branch |
| `projections` | Visualizações, grafo, exportações e integrações — cada caminho para fora do vault |
| `jobs` | `ask`, `digest`, `resurface`: saída do modelo que nunca chega ao `wiki/` |
| `reader` | A superfície somente leitura e com escopo de consumidor em que o visualizador web é construído |
| `gate` | A decisão do hook pré-escrita, apenas biblioteca padrão |

## Como o grafo de conhecimento é formado

O grafo é derivado do vault a cada build — é uma projeção, nunca um artefato armazenado que alguém possa editar.

**Nós** vêm de dois lugares:

- `raw:<sha256>` — um por fonte registrada em `raw/`. Seu `kind` é `raw` e
  seu `label` é o nome de arquivo original. A identidade é o hash de conteúdo, então mover
  um arquivo não cria um novo nó; `bk reconcile` religas o caminho.
- `page:<path>` — um por arquivo markdown sob `wiki/`. Seu `kind` é o
  frontmatter `type` (`source`, `entity`, `concept`, `synthesis`) e seu
  `label` é o frontmatter `title`.

**Arestas** são ambas derivadas, nunca declaradas manualmente:

- `sourced_from` — de uma página para cada hash em seu frontmatter `sources`. Isto
  é o que a verificação de citação do portão apply garante, então a proveniência é
  uma propriedade estrutural do grafo em vez de uma convenção.
- `links_to` — de uma página para outra página, resolvida a partir de `[[wiki-links]]` em
  o corpo por slug. Links irresolvíveis são rejeitados no momento apply, então o grafo
  não tem arestas soltas.

Arestas propostas pelo modelo são armazenadas separadamente desta projeção e unidas na leitura
time — veja [Enriquecimento](./enrichment.md).

## Roteamento de julgamento

Quando a evidência abrange branches, o roteador de julgamento aplica a política mais rigorosa:
`never-ingest` nega a chamada, `local-only` requer Ollama, e o roteamento em nuvem
é permitido apenas quando cada branch contribuinte o permite. Um mapeamento de trabalho pode
definir rotas específicas de privacidade:

```json
{
  "query": {
    "cloud": { "provider": "openai", "model": "gpt-example" },
    "local-only": { "provider": "ollama", "model": "qwen-example" }
  }
}
```

Como a evidência `local-only` só pode chegar ao Ollama, essa rota não deve ser a
mais estreita. Caso contrário, o Ollama aplica seu próprio contexto de 4096 tokens independentemente de
a janela que um modelo anuncia, que é menor do que um prompt digest em um vault
de qualquer tamanho. `providers.ollama.options` é encaminhado literalmente para a API do Ollama
e o padrão é `{"temperature": 0, "num_ctx": 16384}`; valores do operador substituem
por chave. `temperature` fica em `0` por padrão porque a saída do julgamento é
vinculada a esquema e o determinismo é importante.

```json
{
  "providers": {
    "ollama": {
      "base_url": "http://127.0.0.1:11434",
      "options": { "num_ctx": 32768 }
    }
  }
}
```

Um modelo de raciocínio roteado para um provedor compatível com OpenAI pode
gastar todo o orçamento de conclusão pensando e devolver uma resposta vazia.
`providers.<nome>.reasoning` é encaminhado literalmente para controlar isso;
é ausente por padrão, então um modelo que raciocina continua fazendo isso até
que um operador diga o contrário. O formato pertence ao provedor — só a
OpenRouter aceita `effort`, `max_tokens`, `enabled` e `exclude` — por isso é
repassado em vez de modelado aqui. Um endpoint que se recusa a pular o
raciocínio é repetido sem a opção, porque a supressão é uma preferência de custo
e latência, nunca de correção.

```json
{
  "providers": {
    "openrouter": {
      "base_url": "https://openrouter.ai/api/v1",
      "api_key_env": "OPENROUTER_API_KEY",
      "reasoning": { "enabled": false, "exclude": true }
    }
  }
}
```

Uma conclusão vazia é recusada em vez de devolvida. Repassá-la manda o laço de
reparo atrás de uma violação de esquema que nenhuma retentativa corrige; a
recusa nomeia `finish_reason` e a opção acima.

Anthropic, OpenAI, OpenRouter e Ollama são drivers intercambiáveis atrás de um
contrato de trabalho. A neutralidade do provedor é um requisito em vez de uma preferência:
a evidência `local-only` é roteada para Ollama ou não é roteada em absoluto.

---
<!-- doc-tracking -->
- Created: 2026-08-13 09:33
