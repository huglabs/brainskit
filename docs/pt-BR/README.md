*[English version](../README.md)*

# Documentação do brainskit

O [README do projeto](../../README.pt-BR.md) explica o que é o brainskit, como instalá-lo
e os primeiros dez minutos de uso. Tudo abaixo é o detalhe que afogaria aquele documento.

## Usando um vault

| Documento | O que cobre |
|---|---|
| [Primeiros passos](./getting-started.md) | `bk init`, a primeira captura, o contrato da proposta de apply, a estrutura do vault |
| [Referência de comandos](./commands.md) | Todos os comandos, e o que cada código de falha diz para o chamador fazer |
| [O limite de privacidade](./privacy.md) | Consumidores, o que a filtragem cobre, por que uma redação nunca é descrita |
| [Arquivamento e revisão](./filing.md) | `bk ingest`, a fila de propostas, freshness e integridade |

## Estendendo um vault

| Documento | O que cobre |
|---|---|
| [O grafo de código](./code-graph.md) | `bk code`, cobertura de linguagens, o que precisa do extra `code` |
| [Enriquecimento](./enrichment.md) | Arestas propostas por modelo, e as três regras que tornam uma admissível |
| [Integrações persistentes](./integrations.md) | Obsidian, Neo4j, PostgreSQL, vários vaults em um único armazenamento, regras de saída |
| [Servindo um vault](./serving.md) | O visualizador web local e MCP via stdio ou HTTP |
| [Agentes de código](./agents.md) | `bk hooks install`, provando que o write gate realmente protege, o que um watch ignora |

## Entendendo e alterando o motor

| Documento | O que cobre |
|---|---|
| [Arquitetura](./architecture.md) | Camadas, os módulos da aplicação, roteamento de julgamento |
| [Benchmarks](./benchmarks.md) | Cobertura do grafo de código e recuperação LOCOMO, e o que esses números não estabelecem |
| [Desenvolvimento](./development.md) | Configuração local, o gate de entrega, publicação no PyPI |

As convenções de engenharia e as classes de defeito que este código-base já pagou
o preço para resolver estão registradas em [`AGENTS.md`](../../AGENTS.md). Leia antes
de alterar o apply gate, o filtro de privacidade ou o ciclo de vida de uma integração.

---
<!-- doc-tracking -->
- Created: 2026-08-13 09:33
