*[English version](../development.md)*

# Desenvolvimento

O repositório é gerenciado por uv. `uv.lock` fixa o ambiente de desenvolvimento e
`.python-version` fixa o interpretador; nenhum deles restringe um `bk` instalado.

```bash
git clone https://github.com/huglabs/brainskit
cd brainskit

uv sync --group dev              # engine + pytest, ruff, mypy
uv sync --all-extras --group dev # add the Neo4j and PostgreSQL drivers

uv run pytest
uv run ruff check
uv run mypy src
```

Use `-e` durante o desenvolvimento para que `bk` sempre execute a árvore de trabalho:

```bash
uv tool install --force -e '.[integrations]'
```

## O portão de entrega

A entrega é controlada por um wheel que é construído, instalado em um ambiente
descartável e acionado através do contrato CLI real, porque as especificações
de prompt empacotadas, esquemas de saída e modelos não podem ser verificados
na árvore de origem. O portão constrói o sdist primeiro e verifica o wheel
produzido *a partir dele*, que é o artefato que a publicação envia:

```bash
./scripts/verify-wheel.sh
```

Ele verifica que todo recurso empacotado foi enviado, depois executa `init`, `capture`,
`status` e `lint` contra o wheel instalado. Um wheel que importa mas não consegue
inicializar um vault é uma entrega quebrada, portanto o portão falha no comportamento
em vez de na importação.

## O que CI força

`.github/workflows/ci.yml` executa o mesmo portão em cada push e pull request,
em uma ordem deliberada: `ruff` e `mypy` são rápidos e falham em toda a árvore, portanto
controlam os trabalhos mais lentos, e o trabalho wheel é o último porque é o único
que prova o que realmente é enviado. A suite também é executada no Python 3.11, o piso
de `requires-python` — um `bk` instalado deve funcionar lá, não apenas na versão em que
um mantenedor se desenvolveu.

`ruff format --check` está intencionalmente ausente. Este projeto está limpo de lint, não
limpo de formato; adicioná-lo falharia em cada execução em um diff pré-existente em vez
de em qualquer coisa que um contribuidor fez.

## Liberação

As versões são `MAJOR.MINOR.PATCH` em `[project].version`, e cada versão publicada
possui uma tag git anotada `v<version>` no commit do qual foi construída. PyPI rejeita
o re-upload de um nome de arquivo que já armazena, portanto uma versão é permanente e
a tag é o único registro durável de qual árvore a produziu.

A publicação é executada a partir de `.github/workflows/release.yml` em uma tag enviada,
através da PyPI [Trusted Publishing](https://docs.pypi.org/trusted-publishers/) — o
fluxo de trabalho autentica com um token OIDC de curta duração, portanto nenhum token
API existe para vazar ou girar. O fluxo de trabalho recusa publicar quando a tag e
`[project].version` divergem, e executa o portão de entrega antes de enviar qualquer coisa.

```bash
# 1. bump [project].version and add the CHANGELOG entry, then commit
git commit -am 'Release 0.5.0'
# 2. tag the exact commit the artifact will be built from
git tag -a v0.5.0 -m 'brainskit 0.5.0'
# 3. push the commit and the tag together
git push origin main --follow-tags
```

Os mantenedores configuram o editor uma vez, em
[pypi.org](https://pypi.org/manage/account/publishing/), contra o repositório
`huglabs/brainskit`, fluxo de trabalho `release.yml` e ambiente `pypi`.

Para ensaiar o caminho todo sem gastar um número de versão, publique uma pré-versão
(`0.5.0rc1`) — PyPI aceita e `uv tool install brainskit` não a selecionará sem
`--prerelease allow`.

## Convenções

As convenções de engenharia — e as classes de defeito que este código já pagou — são
registradas em [`AGENTS.md`](../AGENTS.md). Leia antes de alterar o portão de aplicação,
o filtro de privacidade ou um ciclo de vida de integração, e registre qualquer nova
classe de defeito lá.

`src/brainskit/infrastructure/codeanalysis/` é fonte de terceiros vendida, mantida
byte-idêntica ao upstream para que uma re-venda seja uma cópia em vez de uma fusão.
É excluído de ruff e mypy por esse motivo; o adaptador que o chama é verificado, que
é onde o limite que realmente importa está. A proveniência está em [`NOTICE`](../../NOTICE).

---
<!-- doc-tracking -->
- Created: 2026-08-13 09:33
- Updated: 2026-08-13 14:11
