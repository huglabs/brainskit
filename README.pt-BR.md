<div align="center">

<img src="./docs/assets/brainskit-mark.svg" width="84" alt="" />

# brainskit `bk`

**A memória do seu agente, com comprovação.**

Local-first · agnóstico em LLM · nada chega à wiki sem proveniência

[![PyPI](https://img.shields.io/pypi/v/brainskit?style=flat-square&color=ee502c&labelColor=0c0c0c&logo=pypi&logoColor=white)](https://pypi.org/project/brainskit/)
[![Python](https://img.shields.io/pypi/pyversions/brainskit?style=flat-square&color=ee502c&labelColor=0c0c0c&logo=python&logoColor=white)](https://pypi.org/project/brainskit/)
[![License](https://img.shields.io/badge/license-Apache--2.0-ee502c?style=flat-square&labelColor=0c0c0c)](./LICENSE)
[![CI](https://img.shields.io/github/actions/workflow/status/huglabs/brainskit/ci.yml?branch=main&style=flat-square&color=ee502c&labelColor=0c0c0c&logo=githubactions&logoColor=white)](https://github.com/huglabs/brainskit/actions/workflows/ci.yml)
[![by HugLabs](https://img.shields.io/badge/by-HugLabs-ee502c?style=flat-square&labelColor=0c0c0c)](https://huglabs.ai)

[English](./README.md) · **Português**

[Começar](./docs/pt-BR/getting-started.md) ·
[Comandos](./docs/pt-BR/commands.md) ·
[Privacidade](./docs/pt-BR/privacy.md) ·
[Arquitetura](./docs/pt-BR/architecture.md) ·
[Todos os docs](./docs/pt-BR/README.md)

<sub>Um projeto de código aberto de <a href="https://huglabs.ai"><b>HugLabs</b></a> — o laboratório de pesquisa aplicada para IA corporativa que funciona.</sub>

</div>

---

<div align="center">

```
██████╗ ██████╗  █████╗ ██╗███╗   ██╗███████╗██╗  ██╗██╗████████╗
██╔══██╗██╔══██╗██╔══██╗██║████╗  ██║██╔════╝██║ ██╔╝██║╚══██╔══╝
██████╔╝██████╔╝███████║██║██╔██╗ ██║███████╗█████╔╝ ██║   ██║
██╔══██╗██╔══██╗██╔══██║██║██║╚██╗██║╚════██║██╔═██╗ ██║   ██║
██████╔╝██║  ██║██║  ██║██║██║ ╚████║███████║██║  ██╗██║   ██║
╚═════╝ ╚═╝  ╚═╝╚═╝  ╚═╝╚═╝╚═╝  ╚═══╝╚══════╝╚═╝  ╚═╝╚═╝   ╚═╝
by HugLabs • Enterprise AI that ships
www.huglabs.ai • open source, Apache-2.0
```

</div>

## Seu agente está escrevendo sua base de conhecimento há seis meses

Agora abra qualquer página e responda uma pergunta: **de onde isso veio?**

Se a resposta é "o modelo disse", você não tem uma base de conhecimento. Você tem
saída do modelo com um nome de arquivo — confiante, bem formatada e infalsificável.
Cada recuperação em cima disso herda isso. Cada decisão posterior a isso herda também.

Uma base de conhecimento que um agente pode escrever livremente para de ser evidência.

## Então brainskit não deixa

Markdown e JSON são a fonte de verdade. SQLite FTS5 é um índice descartável.
Um modelo pode *propor* o que sua base de conhecimento deveria dizer — e apenas
o determinístico `bk apply` pode escrever.

**A recusa é o produto:**

```console
$ bk apply proposal.json
bk: Apply proposal rejected; no files were written
  failures: path: wiki/concepts/context-rot.md, code: citation_mismatch,
            missing_citations: (none),
            undeclared_citations: 0000000000000000000000000000000000000000000000000000000000000000
$ echo $?
2
```

Uma citação que não resolve e o **lote inteiro** é rejeitado. Não
escrito parcialmente. Não escrito com um aviso. Código de saída 2 diz ao chamador para
corrigir a proposta e tentar novamente — e nada no disco se moveu.

Esse é o discurso inteiro. Tudo abaixo é como funciona.

## Seis invariantes, aplicados mecanicamente

Não convenções. Não conselhos de linting. Não um guia de estilo que alguém vai parar
de seguir em março.

| Invariante | Como é aplicado |
|---|---|
| **A evidência bruta é imutável** | Capturas são identificadas por SHA-256 de seus bytes; `bk lint` compara os bytes atuais contra o hash registrado, e `bk reconcile` cura uma movimentação sem reescrever identidade. |
| **Apenas o portão escreve a wiki** | `bk apply` valida esquema, citações, links e novidade para o lote inteiro antes de qualquer página ser substituída; edições diretas na wiki são reportadas por `lint`. |
| **Cada página carrega proveniência** | Uma página declara os hashes de fonte dos quais foi derivada, cada um deve ser citado no corpo, e cada um deve resolver dentro do vault antes que a escrita seja elegível. |
| **Privacidade é um limite declarado** | Chamadores de máquina devem nomear um consumidor (`local`, `cloud`, `human`); o filtro é executado após expansão de grafo, então uma aresta não pode reintroduzir evidência restrita. |
| **Mecânico fica sem LLM** | Captura, indexação, busca, aplicação, exportação e o lint estrutural nunca chamam um modelo. Fluxos de julgamento são separados, ligados a esquema, e roteados pela política mais rigorosa no conjunto de evidência. |
| **Uma escrita é uma unidade de trabalho** | Páginas da wiki, atualização de dados, status do registro, a movimentação de arquivo bruto e a atualização FTS5 ficam visíveis juntas ou são restauradas do diário de transações. |

## Funciona no seu laptop e não pede nada

Nenhum sistema de conta. Nenhuma credencial. Nenhuma rede para qualquer trabalho mecânico.
Uma dependência no núcleo.

Anthropic, OpenAI, OpenRouter e Ollama são drivers intercambiáveis atrás de um
contrato de trabalho — e evidência que você marcou `local-only` vai para Ollama ou não vai
a lugar nenhum.

## Instalar

`bk` opera em um diretório vault, não no projeto a partir do qual é invocado, então
instale-o como uma ferramenta isolada. Ele aterra em `PATH` para cada vault sem
se tornar uma dependência de qualquer projeto.

```bash
uv tool install brainskit     # ou: pipx install brainskit
bk --help
```

Quatro extras são opcionais, porque o núcleo mantém uma única dependência e nenhuma dessas
capacidades é obrigatória para cada vault:

```bash
uv tool install 'brainskit[integrations]'  # Neo4j e drivers PostgreSQL
uv tool install 'brainskit[code]'          # bk code: gramáticas tree-sitter, ~70 MB
uv tool install 'brainskit[code-all]'      # cada linguagem que os extratores podem usar
uv tool install 'brainskit[convert]'       # capturar .docx/.pdf/.pptx via markitdown
```

`code` é o compromisso maior: vendorizar a fonte de análise não vendorizou um
parser, então as gramáticas permanecem wheels compiladas e permanecem uma verdadeira dependência. Um
comando `bk code` sem ela falha com a dica de instalação em vez de um stack
trace, e os testes de código-grafo são pulados em vez de falharem — então um vault que nunca
lê um repositório nunca paga por um. Sem `convert`, uma captura sem-texto é
armazenada verbatim com uma nota "nenhum conversor disponível" em vez de ser recusada.

Instale a árvore de trabalho, uma ref git, ou um wheel construído com o mesmo comando:

```bash
uv tool install /path/to/brainskit
uv tool install 'brainskit @ git+https://github.com/huglabs/brainskit@v0.5.0'
```

Para fixar `bk` a um projeto em vez da máquina, declare-o como uma dependência
e execute-o através do ambiente do projeto:

```bash
uv add brainskit
uv run bk --vault ./my-vault status
```

Nenhum arquivo `.env` é carregado. Segredos de provedor são lidos apenas da variável de
ambiente explicitamente nomeada na configuração do vault.

## Início rápido

```bash
bk init ./my-vault
```

`init` testa a máquina antes de pedir qualquer coisa — se é um
repositório git, o que `$LANG` implica, se ollama está rodando e quais modelos estão
realmente puxados — então pede apenas o que não pode descobrir: para qual propósito é o vault,
qual modelo executa os seis trabalhos de julgamento, e se deve conectar Obsidian, a
UI web local e seu agente de codificação. Se ollama está desligado, avisa e ainda
produz um vault válido; os trabalhos ficam inativos até que um provedor esteja ativo.

```bash
bk --vault ./my-vault capture notes.md --json
bk --vault ./my-vault search "retrieval memory" --consumer local --json
bk --vault ./my-vault context "retrieval memory" --consumer local --json
bk --vault ./my-vault apply proposal.json --json
bk --vault ./my-vault status
```

`context` retorna o pacote de evidência limitado que um agente precisa para construir uma proposta
de aplicação; `apply` valida o lote completo antes de qualquer página ser substituída. O
contrato completo está em [Começar](./docs/pt-BR/getting-started.md).

`bk status` é o vault em uma tela — contagens, branches, atualização de dados, se as
projeções derivadas ainda correspondem às páginas das quais foram construídas, e se as
camadas de aplicação realmente estão rodando:

```console
$ bk --vault ./my-vault status
✓ vault healthy

vault       /home/you/my-vault
sources     45
pending     4
wiki pages  36
indexed     81 documents (updated 2026-08-11T04:06:07.222244+00:00)

————————————————————————— wiki freshness —————————————————————————
fresh    19
review   17
stale    0
unknown  0

—————————————————————————— enforcement ———————————————————————————
layer           status
write_gate      ✓ active
session_status  ✓ active
commit_lint     ✓ active
instructions    ✓ active
```

## Seu agente também não pode contorná-lo

`bk hooks install` conecta o portão em seu agente de codificação como um hook PreToolUse,
então uma escrita sob `wiki/` ou `raw/` é recusada no momento em que é tentada —
não revisada depois, não capturada em um lint que alguém pula.

E `bk doctor` não confia na palavra do portão. Ele *executa* a coisa: um
caminho que o portão deve recusar, um que deve permitir. Um portão que falha aberto porque
`bk` caiu de `PATH` reporta como `not_enforcing` e repete a explicação do portão em si, em vez de
uma marca verde que não guarda nada.

Uma verificação de status que verifica se um arquivo existe não é a mesma que uma que verifica
se um arquivo está rodando.

## Documentação

| | |
|---|---|
| [Começar](./docs/pt-BR/getting-started.md) | Primeiro vault, contrato de proposta de aplicação, layout do vault |
| [Referência de comandos](./docs/pt-BR/commands.md) | Cada comando, e o que cada código de falha diz a um chamador para fazer |
| [O limite de privacidade](./docs/pt-BR/privacy.md) | Consumidores, e por que uma redação nunca é descrita |
| [Arquivamento e revisão](./docs/pt-BR/filing.md) | `bk ingest`, fila de propostas, atualização de dados e integridade |
| [O código grafo](./docs/pt-BR/code-graph.md) | `bk code`: o repositório que um vault documenta, como estrutura |
| [Enriquecimento](./docs/pt-BR/enrichment.md) | Arestas propostas pelo modelo, e as regras que tornam uma admissível |
| [Integrações persistentes](./docs/pt-BR/integrations.md) | Obsidian, Neo4j, PostgreSQL, muitos vaults em uma loja |
| [Servindo um vault](./docs/pt-BR/serving.md) | O visualizador web local, e MCP sobre stdio ou HTTP |
| [Agentes de codificação](./docs/pt-BR/agents.md) | `bk hooks install`, e provando que o portão de escrita realmente guarda |
| [Arquitetura](./docs/pt-BR/architecture.md) | Camadas, módulos da aplicação, roteamento de julgamento |
| [Benchmarks](./docs/pt-BR/benchmarks.md) | Cobertura de código-grafo e recuperação LOCOMO |
| [Desenvolvimento](./docs/pt-BR/development.md) | Configuração local, portão de entrega, lançamento |

## O que o mecanismo implementa

- inicialização de vault orientada por política;
- captura imutável com identidade SHA-256 e reconciliação de registro;
- indexação/busca FTS5, contexto de evidência, lint, visualizações geradas e grafo;
- uma unidade de trabalho recuperável de falha para wiki, atualização de dados, FTS5 e arquivamento bruto;
- portões de esquema, citação, link e novidade em cada `bk apply`;
- arquivamento de propostas aprovadas/rejeitadas durável orientado por política por branch;
- ciclo de vida de atualização de dados (`fresh`, `review`, `stale`) e ressurgimento;
- filtragem de privacidade consciente do consumidor através de expansão de BM25 e grafo;
- saídas de julgamento ligadas a esquema com reparo de feedback automático;
- trabalhos neutros em relação ao provedor com drivers Anthropic, OpenAI, OpenRouter e Ollama;
- modo CLI JSON mais transporte stdio e MCP HTTP autenticado;
- integrações nativas persistentes para Obsidian, Neo4j e grafos PostgreSQL;
- gerenciamento de ciclo de vida com opção de ativar com volumes Docker duráveis para bancos de dados de grafo;
- um visualizador web sem dependências com busca, navegação de grafo, saúde, atualização de dados,
  contagens de branch, fila de revisão e inspeção de fonte/página.

## O limite de privacidade em um parágrafo

Chamadores de máquina devem declarar um consumidor. `cloud` recebe apenas evidência
elegível para nuvem, `local` exclui `never-ingest`, e `human` não aplica restrição —
é o padrão interativo, e um chamador de máquina o recebe apenas nomeando-o.
A filtragem cobre corpos e metadados também, porque um nome de arquivo e seu branch
são em si divulgação, e é executada *após* expansão de grafo então um vizinho
não pode puxar evidência restrita de volta à vista. `search` e `context` reportam
`redacted` como uma contagem e nunca descrevem o que foi retido. Detalhes:
[o limite de privacidade](./docs/pt-BR/privacy.md).

## Status

O contrato do mecanismo é apropriado para teste local. O hábito diário end-to-end
ainda precisa da camada de entrega externa:

| Marco | Estado |
|---|---|
| M0–M3 esqueleto caminhante local | Implementado |
| Sockets locais: stdio/HTTP MCP, pastas monitoradas, API web | Implementado |
| Integrações persistentes Obsidian / Neo4j / PostgreSQL | Implementado |
| Google Drive OAuth e delta polling | Marco do conector |
| Adaptadores gateway-agente para entrega real WhatsApp/Telegram | Marco do conector |
| Agendador de produção | Marco do conector |
| Testes end-to-end contra LLM live e provedores de banco de dados | Planejado |
| Adaptador nativo Kuzu, importadores de corpus-seed | Roteiro posterior |

Nada no lado não implementado é simulado por este repositório: um comando
que precisaria de um conector faltante falha em vez de fingir. Se não está
construído, diz assim e sai com código não-zero.

## Contribuindo

Issues e pull requests são bem-vindos — veja [CONTRIBUTING.md](./CONTRIBUTING.md)
para o portão que toda mudança tem que passar, e [SECURITY.md](./SECURITY.md) para
relatar uma vulnerabilidade em privado.

## Licença

Apache-2.0. Veja [LICENSE](./LICENSE), e [NOTICE](./NOTICE) para o subconjunto
de análise de código vendorizado e sua atribuição.

---

<div align="center">

<a href="https://huglabs.ai">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="./docs/assets/huglabs-dark.png" />
    <img src="./docs/assets/huglabs-light.png" width="168" alt="HugLabs" />
  </picture>
</a>

### Criado por HugLabs. Mantido pela comunidade.

**O laboratório de pesquisa aplicada para IA corporativa que funciona.**

Um laboratório de pesquisa em IA brasileiro e venture studio que transforma ciência
de ponta em sistemas reais para problemas críticos de negócio — seis famílias de
produtos, onze produtos em produção, e uma parceria acadêmica com CEIA-UFG.

Não vendemos capacidades. Vendemos entrega. brainskit começou como a camada de memória
por baixo desse trabalho, e foi lançado como código aberto porque um portão de
proveniência só vale a pena confiar se você consegue lê-lo. Desenvolvimento
dia-a-dia agora acontece aberto, impulsionado pelas pessoas que o executam — veja
[CONTRIBUTING.md](./CONTRIBUTING.md) para se juntar.

[huglabs.ai](https://huglabs.ai) ·
[github.com/huglabs](https://github.com/huglabs) ·
*Rigor acadêmico, prazos de startup.*

</div>
