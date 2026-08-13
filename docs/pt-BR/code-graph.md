*[English version](../code-graph.md)*

# O grafo de código

`bk code` descreve o repositório que um vault documenta, de modo que uma pergunta sobre o
código é respondida a partir de sua estrutura em vez de um grep. É um segundo
grafo, junto ao vault: `bk graph` regenera `graph/graph.json`
a partir da wiki, enquanto `bk code build` extrai `graph/code.json` do código-fonte
da árvore.

Toda leitura do grafo de código usa como padrão `--consumer local`, porque carrega
caminhos de repositório e não deve sair da máquina.

## O que precisa da extra

Apenas duas coisas precisam da extra `[code]`, e precisam de metades diferentes dela:
`build` precisa das gramáticas do tree-sitter para extrair, e `communities`, `cycles`
e `diff` precisam do networkx para a análise vendorizada. Todo o resto — `import`,
`status`, `affected`, `path`, `hubs` — lê o grafo armazenado e funciona na
instalação base, então um grafo extraído em outro lugar pode ser importado e consultado com
nenhuma extra em absoluto. Um comando que precisa dela falha com a dica de instalação em vez
de um stack trace.

`[code]` fixa gramáticas para as treze linguagens mais comuns — Python, JS/TS,
Go, Rust, Java, C/C++, C#, Ruby, PHP, Bash e JSON. Os extractores vendorizados podem
dirigir mais dezesseis (SQL, Swift, Terraform/HCL, Kotlin, Objective-C, Groovy,
PowerShell, Fortran, Lua, Elixir, Julia, Scala, Zig, Verilog, Pascal, DM), e
sem sua gramática um extrator é inacessível. `bk code build` verifica a
varredura antes de executá-la, nomeia as gramáticas que este repositório realmente precisa, e
oferece instalá-las — com o comando que funciona para *este* interpretador,
que nem sempre é `pip`: um ambiente de ferramenta uv não contém um. Recuse
e a construção prossegue, relatando o que não pôde ser analisado em vez de
ter sucesso silenciosamente. Instale uma gramática, ou todas elas antecipadamente:

```bash
uv tool install 'brainskit[code-all]'   # every language, a much larger download
```

A divisão é deliberada — gramáticas são rodas compiladas, e `code-all` grosseiramente
triplica uma instalação já grande para linguagens que a maioria dos repositórios não contém.

## Comandos

```bash
bk code build                     # extract in-process and store graph/code.json
bk code import GRAPH.json         # or take one an external extractor produced
bk code status                    # does the stored graph still describe the tree?
bk code affected SYMBOL           # what breaks if this changes
bk code path FROM TO              # shortest chain of edges between two symbols
bk code hubs                      # the most connected symbols
bk code communities               # structurally cohesive clusters
bk code cycles                    # import cycles among files
bk code diff                      # what changed structurally since the stored graph
```

## Onde digitaliza

`code_root` é lido de `.brain/config.json`, é relativo ao vault, e é
descoberto para cima quando ausente. Uma string vazia explícita significa a raiz do vault,
para um vault que fica no topo do repositório que documenta — a mesma
regra ausente versus vazia que os padrões `ignore` seguem. Os diretórios do vault em si
são excluídos do grafo: um extrator apontado para o repositório
não tem ideia de que uma dessas pastas é o vault fazendo a pergunta, e se deixadas
elas chegam como os nós mais conectados nele.

Dados `PATH`s explícitos, `build` mescla esse subconjunto no grafo armazenado
em vez de substituí-lo, então uma re-extração com escopo não encolhe o grafo para
os caminhos aos quais foi dado.

## Um importador, duas fontes

`build` e `import` compartilham um importador, então um grafo de um extrator externo
é normalizado na entrada em vez de confiável como dado. `communities`,
`cycles` e `diff` são as três perguntas que brainskit não responde sozinho e
são delegadas à análise vendorizada; `affected`, `path` e `hubs` usam
a travessia própria do brainskit, que não precisa de dependência e já responde sob um
`--consumer`.

O subconjunto de análise vendorizada, sua revisão upstream e as mudanças feitas nele
são registrados em [`NOTICE`](../../NOTICE) e no próprio `NOTICE` daquele diretório.
Os números de cobertura estão em [Benchmarks](./benchmarks.md).

---
<!-- doc-tracking -->
- Created: 2026-08-13 09:33
- Updated: 2026-08-13 14:11
