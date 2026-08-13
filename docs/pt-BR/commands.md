*[English version](../commands.md)*

# Referência de comandos

As opções globais vêm antes do subcomando: `--vault PATH` seleciona o vault
(caso contrário, ele é descoberto para cima a partir do diretório de trabalho) e `--json`
muda para saída legível por máquina.

## Mecânico — nunca chama um modelo

| Comando | Propósito |
|---|---|
| `init [PATH] [--config FILE\|-]` | Inicializar um vault com política completa |
| `capture [SOURCE] [--text T] [--title T]` | Capturar um arquivo, URL ou texto literal |
| `status` | Saúde do vault e contagens |
| `doctor` | Saúde, mais uma verificação ao vivo do portão de escrita instalado |
| `reconcile` | Recuperar caminhos de registro após movimentações manuais; descartar atualização de frescor órfãs |
| `reindex` | Reconstruir o índice FTS5 descartável |
| `file` | Mover uma fonte bruta para um ramo |
| `forget ITEM [--force]` | Descartar um registro de fonte cuja arquivo bruto desapareceu do registro |
| `lint [--changed]` | Validar contratos de registro e wiki |
| `search QUERY [--limit N] [--consumer C]` | Busca BM25 FTS5 |
| `context QUERY [--limit N] [--max-chars N] [--consumer C]` | Pacote de evidência limitado |
| `apply PROPOSAL\|-` | Validar e confirmar atomicamente escrita em wiki |
| `gate check-write PATH [--agent A]` | Se uma escrita direta em `PATH` é permitida. Um `PATH` relativo é resolvido contra o diretório atual, como todo outro comando |
| `views` · `graph [--html]` | Regenerar visualizações e o gráfico de conhecimento |
| `enrich apply PROPOSAL\|-` · `enrich list [--consumer C]` · `enrich forget ID` | Arestas inferidas por modelo, protegidas e armazenadas separadamente da projeção |
| `code build [PATH …]` · `code import` · `code status` | Extrair o gráfico do repositório, importar um, re-verificá-lo — `build` precisa de `[code]`. Dados `PATH`s, mescla esse subconjunto no gráfico armazenado em vez de substituí-lo |
| `code affected` · `code path` · `code hubs` | Consultas sobre esse gráfico, na instalação base |
| `code communities` · `code cycles` · `code diff` | Delegado para a análise fornecida — precisa de `[code]` |
| `export --target T [--consumer C]` | Exportar para `json`, `graphml`, `cypher`, `obsidian`, `neo4j`, `postgres`, `kuzu`, `llms-txt` |
| `proposals [--status S]` · `approve ID` · `reject ID --reason R` | Fila de revisão de arquivo |
| `integration configure\|status\|up\|down\|sync NAME` | Ciclo de vida de integração persistente |
| `vaults register\|list\|forget\|sync` | Os vaults nesta máquina, sincronizados em um armazenamento compartilhado |
| `web serve [--host H] [--port P] [--consumer C] [--token-env V]` | Visualizador web em primeiro plano |
| `serve --mcp [--transport stdio\|http] …` | Transportes MCP |
| `watch [--once] [--interval S]` | Capturar novos arquivos nas pastas de origem configuradas, menos `ignore` |
| `schedule` | Mostrar registros de trabalho de hábito configurados |
| `hooks install --agent claude\|codex\|gemini\|opencode [--force]` | Instalar o contrato do agente |

## Julgamento — roteado através de especificações de tarefas e esquemas de saída

| Comando | Propósito |
|---|---|
| `ingest [ITEM] [--all]` | Propor um ramo, depois uma proposta de aplicação válida de esquema |
| `ask QUERY` | Responder de evidência de vault compilado |
| `digest` | Gerar o resumo configurado |
| `resurface` | Trazer à superfície um insight durável |
| `lint --semantic` | Adicionar a passagem de julgamento `lint-semantic` ao relatório estrutural |

## O que uma falha te diz para fazer

Toda recusa carrega um código de máquina estável em `--json` (`error.code`) e por
MCP, junto com a mensagem humanizada. O código existe para que um chamador não tenha que
ler inglês para decidir o que fazer em seguida, porque o próximo passo correto difere:

| `error.code` | Saída | O que significa | O que fazer |
|---|---|---|---|
| `validation_error` | 2 | A solicitação está malformada | Altere a solicitação |
| `conflict` | 2 | A solicitação estava correta; o vault se moveu sob ela | Releia o estado, reconstrua a mesma solicitação, tente novamente |
| `not_configured` | 2 | Esta instalação não pode servir — nenhum modelo mapeado, dependência opcional ausente, variável não definida, integração desligada | Pare de tentar; relatar o que o operador deve configurar |
| `refused` | 2 | Bem-formada, mas um portão a proíbe — aninhamento de vault, uma verificação além de seu limite, um processo que este vault não possui | Altere a circunstância, ou use a saída de emergência que a mensagem nomeia |
| `model_response_invalid` | 2 | A saída de um *provedor* falhou na validação ou o loop de reparo ficou sem tentativas | Tente novamente, aumente o teto de token, ou roteie o trabalho para outro lugar |
| `not_found` | 2 | A coisa nomeada não existe | — |
| `policy_denied` | 3 | O limite de privacidade a recusou | Pergunte com um consumidor que pode vê-la |

Os quatro códigos do meio são estreitamentos de `validation_error`, não substituições:
cada um é uma subclasse de `ValidationError`, então qualquer coisa capturando isso ainda o captura
e nenhum status de saída mudou. `conflict` é reivindicado apenas quando tentar novamente poderia
realmente ter sucesso — uma aplicação rejeitada por uma página obsoleta *e* uma reivindicação
não citada é um `validation_error`, porque reler o arquivo corrige a versão e nunca a reivindicação.

---
<!-- doc-tracking -->
- Created: 2026-08-13 09:32
