*[English version](../getting-started.md)*

# Começando

Instale `bk` primeiro — veja [Install](../README.md#install) no README do projeto.

## Criar um vault

Execute `bk init ./my-vault`. A ferramenta verifica a máquina antes de fazer perguntas — se este é um repositório git, qual idioma `$LANG` implica, se ollama está em execução e quais modelos foram realmente baixados — e então pergunta as três coisas que não consegue descobrir por conta própria:

1. **Para que serve o vault.** Um preset (Work / Personal / Research) nomeia as branches e sua privacidade, ou `Custom` permite nomear as suas. Todo preset mantém uma branch `never-ingest`, porque essa é a única escolha de política que você não pode desfazer: o que foi enviado a um provedor foi enviado.
2. **Qual modelo executa os seis jobs.** Escolhido entre os modelos que ollama relata, nunca de um nome hardcoded — um vault configurado para um modelo que você não tem é um vault cujo cada julgamento falha no primeiro uso. Modelos que ollama relata sem suporte a ferramentas também são listados, esmaecidos e não selecionáveis, para que a contagem no cabeçalho seja o tamanho da lista.
3. **Qualquer coisa mais** — sincronização com Obsidian, a interface web local e a configuração de um agente de codificação, que fica ativado por padrão e escreve `.claude/` mais um bloco `CLAUDE.md` gerenciado para você.

As respostas são validadas no prompt que as produziu e mostradas como um resumo que você pode revisar antes de qualquer coisa ser escrita. Se ollama está inativo ou não tem modelos, `init` avisa e ainda produz um vault válido — os jobs simplesmente ficam inativos até que um provedor esteja ativo.

`init` termina com um bloco **Next**, escrito para funcionar a partir do diretório onde você o executou. Executado no diretório pai, `bk init ./my-vault` imprime:

```text
Next
  → bk hooks install --agent claude --vault my-vault  wire up your agent
  → bk status --vault my-vault                        see vault health
  → bk capture <file> --vault my-vault                add your first note
  → bk ask "..." --vault my-vault                     ask a question
```

`--vault` aparece apenas quando a descoberta a partir do diretório atual não encontraria o novo vault. Quando o vault está aninhado em um repositório git, `hooks install` também recebe `--root <projeto>`, para que os arquivos do agente fiquem onde um agente aberto no projeto os carrega — `bk init my-vault` na raiz de um repositório imprime `bk hooks install --agent claude --root . --vault my-vault`. A linha `hooks install` some quando o onboarding já configurou o agente. Com `--json`, os mesmos passos estão em `result.next`.

Acima do bloco Next, `init` pode imprimir um aviso `!` quando `job_models.ingest` aponta para um modelo ollama com menos de 7B parâmetros: um modelo assim tende a perder o contrato de citação do ingest em fontes mais longas. É um conselho, não uma recusa — aponte `job_models.ingest` para um modelo maior em `.brain/config.json` se o ingest falhar. Com `--json`, os avisos estão em `result.warnings`.

As setas do teclado controlam as seleções. Fora de um terminal `init` se recusa a responder suas próprias perguntas com padrões que ninguém viu, então a automação usa o caminho de configuração — imprima uma política completa, edite se quiser, e passe novamente:

```bash
bk init --print-config > policy.json          # add --preset personal|research
bk init ./my-vault --config policy.json --json
bk --vault ./my-vault capture notes.md --json
bk --vault ./my-vault reindex --json
bk --vault ./my-vault search "retrieval memory" --consumer local --json
```

Nenhum arquivo `.env` é carregado. Os segredos do provedor são lidos apenas da variável de ambiente explicitamente nomeada na configuração do vault.

## Escrevendo no wiki

Uma proposta de `apply` é um documento JSON:

```json
{
  "operations": [
    {
      "action": "upsert",
      "kind": "concept",
      "slug": "compiled-memory",
      "title": "Compiled memory",
      "aliases": ["memória compilada"],
      "source_hashes": ["<64-char sha256>"],
      "body": "Evidence-backed text.[^source:<64-char sha256>]",
      "links": [],
      "base_hash": null
    }
  ]
}
```

`bk context QUERY --consumer local --json` fornece o pacote de evidência que um agente precisa para criar essa proposta. `bk apply proposal.json --json` valida o lote completo antes de qualquer página wiki ser substituída. Para atualizações, `base_hash` deve corresponder à versão da página retornada por `context`; novas tentativas com o mesmo `proposal_id` e payload são idempotentes, enquanto a reutilização de chave com outro payload é rejeitada. Um commit multi-página interrompido é revertido quando o vault é aberto novamente.

O arquivamento usa a mesma unidade de trabalho: páginas wiki, atualização da página, status do registro/fonte, movimento de arquivo bruto e o índice SQLite ficam visíveis juntos ou são restaurados do diário de transações. A atualização do índice é incremental, portanto um apply normal não precisa fazer uma reconstrução completa.

## O esquema da página

`.brain/schema.json` é validado como o rascunho JSON Schema declarado por seu URI `$schema`. O gate suporta o vocabulário completo implementado por `jsonschema` para esse rascunho, incluindo combinadores, condicionais, formatos, `$defs`, `$ref` local, `dependentRequired` e `unevaluatedProperties`, antes de aplicar os invariantes de proveniência, citação, link e campo reservado do brainskit.

A recuperação remota de `$ref` é deliberadamente negada: um esquema de vault não pode causar uma solicitação de rede implícita ou vazar dados de política local. Agrupe esquemas referenciados em `$defs` local.

## Layout do vault

`bk init` estrutura todos os diretórios em que o motor coloca arquivos, portanto um tipo de página nunca pode estar em um lugar que o vault não possui:

```text
my-vault/
├── raw/                     evidência imutável, identificada por SHA-256
│   ├── _inbox/              zona de aterrissagem antes de uma decisão de arquivamento
│   ├── _assets/
│   └── <branch>/            um diretório por branch configurada
├── wiki/                    a superfície compilada; escrita apenas pelo gate
│   ├── sources/  entities/  concepts/  syntheses/
│   ├── index.md             páginas do sistema, mantidas pelo motor
│   └── log.md
├── views/map/  views/domains/   navegação gerada
├── graph/                   graph.json gerado, e code.json de bk code build
├── output/                  escrito por jobs de julgamento e exports
│   ├── digests/<data>.md    bk digest
│   ├── resurface/<data>.md  bk resurface
│   ├── answers/<data>-<slug>.md   bk ask --save (e a caixa "salvar" do visualizador)
│   └── export-<alvo>.<ext>        bk export --target json|graphml|cypher|…
└── .brain/                  política e estado durável
    ├── config.json          branches, provedores, ignore, integrações (sem segredos)
    ├── schema.json          esquema de página de propriedade humana, aplicado por apply/lint
    ├── registry.json        hash da fonte → caminho e status
    ├── freshness.json       hashes de página aplicados e estado de ciclo de vida
    ├── proposals.json       propostas de arquivamento pendentes
    ├── applied.json         chaves de idempotência para applies executados
    ├── integration-state.json   PIDs, containers, pontos de verificação de sincronização
    └── index.db             índice FTS5 descartável (git-ignored)
```

`.brain/schema.json` é seu para editar. Tudo mais em `.brain/` é estado do motor: altere executando um comando, não com um editor de texto.

---

Próximo: a [referência de comandos](./commands.md), ou [o limite de privacidade](./privacy.md) antes de apontar qualquer coisa para um modelo em nuvem.

---
<!-- doc-tracking -->
- Created: 2026-08-12 14:47
- Updated: 2026-08-13 09:32
