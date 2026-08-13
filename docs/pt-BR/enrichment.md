*[English version](../enrichment.md)*

# Enriquecimento

Um agente já pode enriquecer o grafo deste cofre sem maquinaria especial:
propor uma página de wiki cujo corpo cite suas fontes, deixar que `bk apply` a valide, e
`bk graph` derivará as arestas `sourced_from` e `links_to` do que foi
escrito. A aresta então é uma *consequência de uma afirmação citada*, e toda garantia
é mantida porque a afirmação carrega hashes de fontes e esses hashes carregam branches.

`bk enrich` é para o relacionamento que não quer ser uma página — "essas
duas entidades são a mesma", "este conceito substitui aquele" — onde a
afirmação *é* a aresta. Três regras tornam isso admissível, e todas três vêm de
invariantes que já existiam:

| Regra | Por que não é negociável |
|---|---|
| **Nunca entra em `graph/graph.json`** | Esse arquivo é uma projeção: `bk graph` a reescreve a partir do wiki toda vez, portanto uma aresta escrita lá é destruída na próxima compilação. O enriquecimento fica em `.brain/enrichment.json` e é unido na leitura. |
| **Toda aresta nomeia sua evidência** | O filtro de privacidade decide pela branch em que um *registro de fonte* vive. Uma aresta sem nada por trás dela é inclassificável, e o filtro roda após a expansão do grafo precisamente para que uma aresta não possa puxar um nó restrito de volta à vista. Uma aresta que não consegue nomear sua proveniência é recusada, não armazenada e escondida. |
| **Marcada onde quer que apareça** | `provenance: "model"`, com arestas derivadas rotuladas como `"derived"`, então nenhum leitor precisa descobrir quais arestas foram extraídas e quais foram argumentadas. |

Uma aresta herda a política **mais rigorosa** entre as fontes das quais foi derivada — a mesma regra que o roteador de julgamento aplica a evidências que abrangem branches,
compartilhada como uma função em vez de escrita duas vezes. Uma fonte `never-ingest`
retém a aresta inteira. A proveniência que não resolve mais falha fechada
(tratada como `never-ingest`) e é reportada por `bk lint` como
`enrichment.unresolved_source`, para que possa ser reparada em vez de ficar
invisível.

```json
{
  "edges": [
    {
      "source": "page:wiki/concepts/compiled-memory.md",
      "target": "page:wiki/concepts/provenance.md",
      "relation": "supersedes",
      "derived_from": ["<64-char sha256>"],
      "model": "qwen2.5:3b",
      "note": "proposed during a digest run"
    }
  ]
}
```

```bash
bk --vault ./my-vault enrich apply proposal.json
bk --vault ./my-vault enrich list --consumer local
bk --vault ./my-vault enrich forget 1eeda80f
```

O portão espelha `bk apply`: todo o lote é validado antes que qualquer um seja
armazenado, um endpoint que não é um nó no grafo é recusado da mesma forma que um
`[[wiki-link]]` não resolvido é, e a identidade é a tríplice `(source, relation, target)` — então um agente que re-executa propõe uma aresta, não duas.

**O que isso não faz.** Não infere nada por conta própria; é um portão para
afirmações que um modelo já fez. E o [benchmark LOCOMO](./benchmarks.md) é
a razão para ter cuidado com o nível acima: o grafo extraído por LLM do graphify
atingiu um teto de cobertura de 0,575 porque a extração de entidades *descarta*.
O enriquecimento que adiciona arestas sobre um corpus que não manteve completamente herda esse
teto.

---
<!-- doc-tracking -->
- Created: 2026-08-13 09:33
