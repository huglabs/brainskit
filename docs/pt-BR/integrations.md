*[English version](../integrations.md)*

# Integrações permanentes

Toda integração é opcional e armazenada em `.brain/config.json`; pontos de verificação de ciclo de vida e sincronização são armazenados em `.brain/integration-state.json`. Segredos nunca são persistidos. A configuração armazena apenas o nome de uma variável de ambiente. `bk integration status` combina a política durável com o estado ativo de processos/contêineres.

Todos os recursos estão disponíveis por meio da CLI JSON e das ferramentas MCP `integration_configure`, `integration_status`, `integration_up`, `integration_down` e `integration_sync`.

## Obsidian

A sincronização Obsidian é baseada em manifesto. Ela copia o `wiki/`, `views/` e `graph/graph.json` gerados para o cofre selecionado e remove apenas os arquivos que o brainskit gerenciava anteriormente. Conteúdo Obsidian de propriedade do usuário nunca é deletado. Evidência bruta é excluída a menos que `--include-raw` seja explicitamente selecionado.

```bash
bk --vault ./my-vault integration configure obsidian \
  --enable --external --path "$HOME/Obsidian" --subdirectory brainskit
bk --vault ./my-vault export --target obsidian
bk --vault ./my-vault integration status obsidian
```

Aponte `--path` para o próprio cofre brainskit para uso in-place do Obsidian. Nesse modo, brainskit cria apenas o `.obsidian/app.json` mínimo quando ausente e não duplica os arquivos de conhecimento.

## Neo4j

Neo4j usa o driver Python oficial e escreve nós `BrainskitNode` mais relacionamentos `SOURCED_FROM` e `LINKS_TO` em uma transação de banco de dados única. Esta é uma transmissão Bolt real, não uma exportação de arquivo Cypher. Todo nó é nomeado com um ID de cofre estável, portanto uma atualização substitui apenas o subgráfico desse cofre e sincronizações repetidas são idempotentes. Pode se conectar a um serviço de propriedade do operador (`--external`) ou criar um serviço Docker (`--managed`) cujos dados sobrevivem a parar/iniciar em `.brain/services/neo4j/data`.

```bash
export BRAINKIT_NEO4J_PASSWORD='use-a-secret-manager-in-production'
bk --vault ./my-vault integration configure neo4j \
  --enable --managed --uri bolt://127.0.0.1:7687 --user neo4j \
  --password-env BRAINKIT_NEO4J_PASSWORD --database neo4j --consumer local
bk --vault ./my-vault integration up neo4j
bk --vault ./my-vault integration sync neo4j
bk --vault ./my-vault integration down neo4j
```

## Gráfico PostgreSQL

O alvo PostgreSQL é um PostgreSQL nativo e portável: um gráfico `nodes`/`edges` enriquecido com JSONB, colunas de adjacência indexadas, integridade referencial e uma função SQL recursiva `graph_walk(start_node, max_depth)`. Não requer uma extensão de gráfico. O modo gerenciado executa PostgreSQL em Docker com dados duráveis em `.brain/services/postgres/data`; o modo externo lê um DSN da variável de ambiente nomeada.

Um esquema contém quantos cofres você apontar para ele. Cada linha carrega o `vault_id` do cofre que a escreveu, e uma sincronização deleta apenas as linhas desse cofre, portanto atualizar um cofre nunca toca em outro — incluindo um cofre de propriedade de um aplicativo diferente compartilhando o esquema. Ambas as tabelas indexam `vault_id`.

Como o esquema é compartilhado, o `id` armazenado é nomeado da mesma forma que a exportação Neo4j nomeia o seu: `<vault_id>:<natural id>`. O id natural não se perde — `properties` mantém o nó intacto, portanto `properties->>'id'` é o id sem prefixo (`page:wiki/index.md`, `raw:<content hash>`), e em `edges`, `properties->>'source'` e `properties->>'target'` igualmente. Filtre por coluna e leia a chave natural do JSONB:

```sql
SELECT properties->>'id' AS id, label, path
FROM brainskit.nodes WHERE vault_id = $1 AND kind = 'wiki';
```

`graph_walk` não precisa de argumento de cofre e não leva nenhum: como cada id carrega o prefixo de seu cofre, uma caminhada não pode sair do cofre em que começou. Passe a id prefixada armazenada, não a natural.

Atualizar uma implantação existente não requer migração manual. A sincronização adiciona a coluna `vault_id` se as tabelas forem anteriores a ela, adota as linhas já existentes no cofre que executa essa primeira sincronização — seguro porque o comportamento anterior truncava ambas as tabelas em cada atualização, portanto o que está em disco é exatamente a última sincronização completa de um cofre — e as substitui na mesma execução.

```bash
export BRAINKIT_POSTGRES_PASSWORD='use-a-secret-manager-in-production'
bk --vault ./my-vault integration configure postgres \
  --enable --managed --password-env BRAINKIT_POSTGRES_PASSWORD \
  --user brainskit --database brainskit --schema brainskit --port 5432 \
  --consumer local
bk --vault ./my-vault integration up postgres
bk --vault ./my-vault export --target postgres
bk --vault ./my-vault integration down postgres
```

Para um serviço existente:

```bash
export BRAINKIT_POSTGRES_DSN='postgresql://user:password@host/database'
bk --vault ./my-vault integration configure postgres \
  --enable --external --dsn-env BRAINKIT_POSTGRES_DSN \
  --schema brainskit --consumer cloud
bk --vault ./my-vault integration sync postgres
```

`bk integration up` aguarda o serviço aceitar conexões da mesma forma que um cliente faria, e exige que ele permaneça ativo antes de relatar `ready` — PostgreSQL responde em seu socket unix do servidor temporário que executa durante `initdb`, que depois desliga e reinicia. Um primeiro boot também tem que fazer chown no diretório de dados local do cofre, que é lento em bind mounts do macOS, então o prazo é 300 segundos e pode ser aumentado por integração com `ready_timeout_seconds` nas opções armazenadas.

## Muitos cofres, um armazenamento

`bk integration sync` sincroniza o cofre para o qual foi apontado. Quando um operador executa vários aplicativos, cada um com seu próprio cofre, `bk vaults` mantém a lista e sincroniza-os como um conjunto, portanto o gráfico compartilhado é a união de todos eles.

Não há etapa de descoberta, e isso é deliberado: os cofres vivem em projetos não relacionados, e uma varredura do sistema de arquivos seria lenta, perderia qualquer coisa fora das árvores para as quais foi apontada e encontraria checkouts, cópias e backups que nunca devem ser sincronizados em um armazenamento compartilhado sob sua própria identidade. A lista é declarada uma vez e vive em `$XDG_CONFIG_HOME/brainskit/vaults.json` (padrão `~/.config/brainskit/vaults.json`), arquivo `0600` dentro de um diretório `0700`. Ele contém caminhos e rótulos e nada mais — a mesma regra que a configuração do cofre segue, onde apenas o *nome* de uma variável de ambiente é sempre armazenado.

```bash
bk vaults register ./app-one --label app-one   # PATH defaults to the vault found from the cwd
bk vaults register ./app-two
bk vaults list                                 # label, path, whether it is still there, vault_id
bk vaults sync --target postgres               # --target defaults to postgres
bk vaults forget app-two                       # unregisters only; the vault's files are untouched
```

Cada cofre mantém sua própria política. Um cofre que não habilitou o alvo é **ignorado**, não habilitado em seu nome, e uma falha em um cofre não interrompe o resto — um disco desmontado ou um serviço inativo é relatado contra apenas esse cofre:

```json
{"target": "postgres", "count": 4, "ok": 2, "skipped": 1, "failed": 1,
 "vaults": [{"label": "app-one", "vault_id": "2e2389340edfb82b1fe52ba9", "status": "ok", "result": {}},
            {"label": "app-optout", "status": "skipped", "reason": "postgres is not enabled in this vault's policy"},
            {"label": "app-gone", "status": "failed", "code": "not_found", "reason": "Not a brainskit vault"}]}
```

Exit é `1` quando qualquer cofre falha e `0` quando todo cofre sucede ou é ignorado, portanto uma execução agendada pode se ramificar apenas no status. `list` relata um cofre cuja diretório foi deletado em vez de falhar, e ainda imprime seu `vault_id` — que é o que você precisa para encontrar as linhas que deixou em um esquema compartilhado antes de executar `bk vaults forget`.

`bk vaults sync` não leva `--consumer`, pela razão de `export` recusar um em um alvo de integração, e mais uma: uma sincronização atualiza deletando as linhas do cofre primeiro, portanto uma execução restrita silenciosamente substituiria o que um armazenamento compartilhado já mantém com menos, em todos os cofres registrados de uma vez. Defina a limite por cofre com `bk --vault <path> integration configure <target> --consumer`.

Este grupo é apenas CLI. Diferentemente das ferramentas `integration_*`, não é exposto sobre MCP: um servidor MCP é iniciado para um cofre e responde sob o limite declarado desse cofre, portanto uma ferramenta que alcançasse cofres não relacionados alargaria o limite concedido ao chamador.

## Egresso carrega a limite

`consumer` é obrigatório para Neo4j e PostgreSQL e opcional para Obsidian, onde o padrão é `local`. `cloud` exporta apenas evidência elegível para nuvem; `local` também permite evidência apenas local, mas sempre redige `never-ingest`. O filtro é executado após a expansão do gráfico, portanto as arestas não podem reintroduzir nós restritos.

Cada egresso leva o mesmo limite, incluindo os alvos de arquivo:

```bash
bk --vault ./my-vault export --target json                    # local (default)
bk --vault ./my-vault export --target cypher --consumer cloud
bk --vault ./my-vault export --target llms-txt --consumer human
```

`--consumer` tem o padrão `local`, portanto uma exportação nunca emite evidência `never-ingest` a menos que `human` seja nomeado deliberadamente. Passar para `--target obsidian`, `neo4j` ou `postgres` é rejeitado em vez de aplicado: esses alvos carregam seu próprio consumidor configurado, e silenciosamente o sobrescrever poderia ampliar o limite além do que a integração foi configurada para permitir.

---
<!-- doc-tracking -->
- Created: 2026-08-13 09:34
