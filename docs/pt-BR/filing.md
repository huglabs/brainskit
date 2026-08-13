*[English version](../filing.md)*

# Arquivamento e revisão

`bk ingest` primeiro propõe uma branch de destino e depois produz uma proposta de aplicação válida no esquema. A política de destino configurada controla o resultado:

- `auto+digest-review`: arquiva e aplica imediatamente, retendo o registro de auditoria;
- `approve-each`: armazena a proposta sem mover ou escrever nada.

```bash
bk --vault ./my-vault ingest --all --json
bk --vault ./my-vault proposals --status pending --json
bk --vault ./my-vault approve <proposal-id> --json
bk --vault ./my-vault reject <proposal-id> --reason "not useful" --json
```

Trabalhos de julgamento são validados contra `jobs/_output-schemas/`. Saída de modelo inválida é retentada com feedback de validação estruturado; nenhuma resposta codificada é substituída.

## Atualização e integridade

Páginas aplicadas são rastreadas em `.brain/freshness.json`. Uma captura relacionada nova marca páginas afetadas para revisão, o limite de idade configurado marca páginas como desatualizadas, e `bk resurface` seleciona um insight durável através do provedor configurado. `bk lint` relata mutação de fonte bruta, edições diretas de wiki fora do portão de aplicação, proveniência não resolvida, links quebrados e páginas desatualizadas.

Atualização é indexada por caminho, então uma página deletada fora do portão deixa uma entrada que nunca pode ser revivida. `bk lint` a relata como `freshness.orphaned`, `bk status` para de contabilizá-la, e `bk reconcile` a remove — o mesmo comando que religar uma fonte movida pelo seu hash.

---
<!-- doc-tracking -->
- Created: 2026-08-13 09:32
