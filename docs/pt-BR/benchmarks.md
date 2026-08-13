*[English version](../benchmarks.md)*

# Benchmarks

`benchmarks/` mede duas coisas que o engine afirma: que o grafo de código indexa o que diz ser capaz, e que a recuperação do vault encontra sobre o que uma pergunta realmente trata. Ambas são reproduzíveis a partir de um checkout limpo.

**Cobertura, não contagem de nós.** Um grafo pode crescer enquanto uma linguagem inteira cai silenciosamente — o que é exatamente o que aconteceu aqui uma vez, e o motivo da métrica de destaque ser *arquivos que produziram pelo menos um nó ÷ arquivos cuja extensão tem um extrator*. Uma contagem de nós teria chamado essa compilação de sucesso.

## Grafo de código

Dez repositórios fixados por commit, abrangendo as linguagens que os extractors enviados afirmam, mais um fixture isolado de 26 linguagens que roda dentro da suite de testes sem rede.

| | |
|---|---|
| **Cobertura** | **100%** em todos os dez repositórios e o fixture |
| Corpus | 1,766 files · 26,706 nodes · 54,745 edges · 21.7 MB of graph |
| Custo | 30.2 s total (~58 files/s), no LLM calls |
| Maior | commons-lang, 625 files in 13.6 s |

Omissões deliberadas são excluídas do denominador. `extract_json` rejeita JSON em forma de dados propositalmente, e contar isso como falha uma vez fez um repositório ler como 82.4% coberto quando cada arquivo de origem que possuía tinha de fato sido analisado.

```bash
python benchmarks/run.py                    # hermetic fixture, seconds
python benchmarks/run.py --corpus           # clone and measure the ten repositories
python benchmarks/run.py --corpus --check   # fail on regression against baseline.json
```

## Recuperação LOCOMO

Resposta a perguntas em longas conversas, pontuadas contra as mudanças de evidência ouro que LOCOMO envia — portanto nenhum juiz está envolvido e nenhuma escolha de modelo pode confundir o número. Ambos os sistemas rodam por **um** harness: as mesmas dez conversas, as mesmas 1,536 questions, a mesma unidade de recuperação (um documento por turno de diálogo), o mesmo scorer, o mesmo k.

| Sistema | recall@10 | ceiling | ranking eff. | MRR | index | query |
|---|---|---|---|---|---|---|
| **brainskit** | **0.519** | 1.000 | 0.519 | 0.376 | 0.6 min · no LLM | 4 ms |
| graphify | 0.302 | 0.575 | 0.525 | 0.185 | 58.4 min · ~3.3M LLM tokens | 132 ms |

`ceiling` é o melhor recall que um ranker *perfeito* poderia alcançar sobre o que cada sistema realmente armazenou, e é a coluna que torna o resultado legível. brainskit indexa cada turno, portanto nada está fora do alcance e sua pontuação é puramente ranking. graphify condensa ~590 turnos em ~70 entidades por conversa, deixando **536 de 1,536 questions** sem nenhuma mudança de evidência em seu grafo — pontuado como zero antes do ranking começar. `ranking eff.` é recall ÷ ceiling: medido contra o que cada sistema manteve, os dois estão empatados (0.525 against 0.519). **A lacuna é cobertura, não ranking.**

```bash
# LOCOMO's release is not vendored
git clone --depth 1 https://github.com/snap-research/locomo /tmp/locomo
cp /tmp/locomo/data/locomo10.json benchmarks/memory/

python benchmarks/memory/run_locomo.py --limit 300        # brainskit alone
python benchmarks/memory/run_locomo_graphify.py --index   # build graphify's graphs
python benchmarks/memory/run_locomo_graphify.py           # both, one harness
```

**O que esses números não estabelecem.** graphify pontua 0.497 em seu próprio harness publicado e 0.302 aqui, portanto isso não está reproduzindo essa configuração: a unidade de documento por turno foi escolhida para tornar a pontuação exata contra a evidência de nível de turno do LOCOMO, e essa escolha favorece um sistema recuperando turnos enquanto desfavorece um recuperando entidades. A recuperação de nível de turno não é aquilo para que um grafo de entidade é construído. O mesmo harness é o que torna dois números comparáveis; não é o que torna um deles um veredicto.

Duas advertências adicionais, ambas aprendidas cometendo-as primeiro. Uma amostra de 300 perguntas do LOCOMO leva uma faixa de ~95% de aproximadamente ±0.06 — ampla o suficiente para que uma descoberta inicial "graphify classifica melhor" (+0.052 at n=383) desaparecesse para +0.006 na população completa. E o resultado do graphify depende inteiramente do modelo que o indexa: pilotado com um modelo local de 3B extraiu duas entidades de trinta turnos, portanto qualquer figura para um sistema com suporte de LLM é uma figura sobre esse LLM. As execuções acima usam `claude-cli`.

---
<!-- doc-tracking -->
- Created: 2026-08-13 09:36
