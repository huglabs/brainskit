*[English version](../agents.md)*

# Agentes de código

`bk hooks install` ensina o contrato do vault a um agente em vez de apenas esperar que ele o infira:

```bash
bk --vault ./my-vault hooks install --agent claude
```

Instala `.claude/skills/brainskit/SKILL.md`, acrescenta um bloco gerenciado ao arquivo de instruções do agente (`CLAUDE.md`, ou `AGENTS.md`/`GEMINI.md` para outros agentes) que cobre como o grafo é formado, onde a fronteira de privacidade se aplica e quais comandos podem escrever, e instala um hook `pre-commit` que executa `bk lint` quando o workspace é um repositório git.

Também executa o primeiro `bk code build` a si mesmo, no mesmo processo, na mesma execução.
Sem isso, `bk code status` continuaria reportando `missing` até que um agente notasse a linha `code build` na tabela de comandos da skill e a executasse sem ser solicitado — nada mais neste caminho jamais o solicita. A compilação é de melhor esforço: um vault que nunca instalou o extra `code` recebe a mesma dica de instalação que `bk code build` já daria por conta própria, reportada em `code_graph` e em stderr, não uma falha de onboarding. Passe `--skip-code-build` para deixar o grafo exatamente como `bk code status` o encontra — para um vault que documenta algo diferente de um repositório de código, ou quando uma primeira extração lenta não deve bloquear o onboarding.

Tudo o que escreve é seguro para re-executar. O bloco de instruções é delimitado por `<!-- brainskit:start -->` / `<!-- brainskit:end -->` e substituído no mesmo lugar, então suas próprias instruções mantêm seu conteúdo e posição. Uma skill existente ou um hook `pre-commit` pré-existente é reportado em vez de sobrescrito; passe `--force` para substituí-los. Um workspace sem git ainda instala tudo o mais.

## Verificando que a porta realmente guarda

`bk status` reporta que cada camada de proteção está instalada e registrada.
Essa não é a mesma afirmação que "uma escrita direta em `wiki/` é recusada", e as duas divergem silenciosamente: o script hook falha aberto propositalmente — sem `python3`, sem `bk` no PATH, um vault inacessível, um bit executável perdido — e cada um desses faz com que saia com 0 em uma escrita que foi instalado para negar, enquanto `status` continua reportando a camada como ativa.

Então `bk doctor` o executa. Envia à porta um caminho que ela deve recusar e outro que deve permitir, usando o mesmo payload que Claude Code envia, e reporta o que realmente aconteceu sob `enforcement.write_gate_probe`:

| `state` | Significado |
|---|---|
| `enforcing` | uma escrita em `wiki/` foi recusada e uma escrita comum não foi |
| `not_enforcing` | o hook está instalado e deixou uma escrita protegida passar |
| `over_blocking` | recusou uma escrita comum fora do vault também |
| `unknown` | o hook não pôde ser executado de jeito nenhum |
| `absent` | nenhuma porta está instalada — uma escolha, não uma falha |

Ambas as sondas são apenas decisões: `gate check-write` não escreve nada e nenhum arquivo de sondagem é criado. Quando o hook falha aberto, ele se explica em stderr, e essa sentença é repetida como `hook_said` porque nomeia a peça faltante melhor do que um código de saída pode. `doctor` reporta `healthy: false` para cada estado exceto `enforcing` e `absent` — uma porta instalada que não guarda é pior que nenhuma, porque tudo o mais continua reportando sucesso.

Um repositório cujo `core.hooksPath` aponta para algum lugar diferente de `.git/hooks` — o que Husky define e o que qualquer repositório pode definir globalmente — não recebe hook escrito algum. Git nunca leria, então instalar um ali deixaria um arquivo que parece instalado e nunca executa. A recusa nomeia o diretório que git realmente usa e a linha para adicionar ao seu próprio `pre-commit`, e `commit_lint` é reportado como inativo por ambos `bk hooks install` e `bk status` até você conectá-lo. `--force` não sobrescreve isso: decide se deve substituir um hook existente, não em qual diretório git executa.

Um `.claude/settings.json` trazido de outro lugar — uma instalação anterior em um `--root` diferente, ou um `.claude/` copiado em massa de outro projeto — tem suas entradas `brainskit-gate`/`brainskit-status` obsoletas substituídas, não deixadas rodando ao lado das novas: a chave de idempotência é a identidade do hook (seu nome de template), não o caminho literal do comando, então um comando apontando para um vault ou workspace diferente é reconhecido como obsoleto e podado. Reportado em `settings.pruned` e stderr. Ferramentas não relacionadas registradas no mesmo evento nunca são tocadas — apenas um comando cujo nome corresponde a `brainskit-gate.sh`/`brainskit-status.sh` é considerado obsoleto.

## O vault nem sempre é o workspace

Um agente lê `.claude/` e seu arquivo de instruções do projeto em que foi aberto. Quando o vault é um diretório dentro daquele projeto, esses são dois lugares diferentes, então nomeie o projeto com `--root`:

```bash
bk --vault ./docs/brain hooks install --agent claude --root .
```

`--root` recebe a configuração do agente — `.claude/`, o arquivo de instruções e o hook `pre-commit` git — enquanto o vault mantém `.brain/` e o caminho do vault incorporado nos scripts do hook. O padrão é o próprio vault, o que um vault independente quer.

Errar isso costumava ser silencioso, e o silêncio é a parte cara: cada arquivo chega, o resumo parece sucesso, e nenhum hook é jamais carregado.
Então uma instalação que repetiria esse erro — um vault sem configuração de agente própria, aninhado dentro de um diretório que tem — diz isso em stderr e nomeia a flag que o corrige:

```text
bk: WORKSPACE - everything installed, nothing will load:
      The vault is not a project root, so an agent opened on /path/to/project
      will never load what was just installed here.
      Reinstall with --root /path/to/project
```

O workspace resolvido é registrado em `.brain/agent-<agent>.json`, porque nada mais no disco se lembra e `bk status` tem que olhar no mesmo lugar onde o instalador escreveu. Um adaptador escrito antes desse campo existir volta para o vault, então uma instalação existente continua reportando exatamente como fazia.

## O que um watch não capturará

`bk watch` percorre cada pasta de origem configurada e captura o que encontra, e uma captura não pode ser desfeita: uma origem é identificada pelo hash de seus bytes e `raw/` é imutável. Então o percurso é filtrado por `ignore` em `.brain/config.json`, uma lista de globs de shell correspondidos a cada segmento de caminho:

```json
{
  "ignore": ["node_modules", ".git", "__pycache__", "dist", "*.log", "docs/build"]
}
```

Um padrão sem separador poda aquele diretório em qualquer lugar que apareça, então `node_modules` custa uma comparação em vez de uma stat por arquivo dentro dele. Um padrão com um é ancorado na pasta de origem, então `docs/build` exclui aquela árvore e deixa cada outro `build` sozinho. A correspondência não diferencia maiúsculas de minúsculas, porque o alvo principal é um sistema de arquivos que não diferencia.

`bk init` oferece os padrões — metadados de controle de versão, diretórios de dependência e compilação, editor e detritos do SO — pré-preenchidos, para que possam ser editados em vez de descobertos depois. Um vault criado antes desse campo existir herda esses padrões; um vault que armazena `[]` disse "ignore nothing" e o obtém.
`watch --json` reporta `ignored` ao lado de `created`, contando árvores podadas uma vez em vez de por arquivo dentro delas.

O próprio diretório do vault é sempre excluído, então uma pasta de origem que contém o vault não pode re-capturar `raw/` para si mesma.

---
<!-- doc-tracking -->
- Created: 2026-08-13 09:34
