<!-- Stage 03. Component relationships and key flows for 02-spec.md. -->

# Components — what each task touches

Companion to [`../02-spec.md`](../02-spec.md). The last section is the one to read
before dispatching parallel work.

## 1. The leak path — task 1.1, before and after

```mermaid
sequenceDiagram
    autonumber
    participant A as agent / bk search
    participant R as retrieval.py
    participant P as _evidence_privacy
    participant REG as registry

    Note over A,REG: BEFORE — the hash no longer resolves
    A->>R: search(consumer="cloud")
    R->>P: (hit, content, records, config)
    P->>REG: sources = [h1]; is h1 in records?
    REG-->>P: no
    P->>P: generator filters it out → empty
    P-->>R: strictest_privacy(∅) = CLOUD
    R-->>A: count=1 · redacted=0 · privacy="cloud"
    Note right of A: never-ingest body served,<br/>and affirmatively mislabelled

    Note over A,REG: AFTER — task 1.1
    A->>R: search(consumer="cloud")
    R->>P: (hit, content, records, config)
    P->>REG: sources = [h1]; is h1 in records?
    REG-->>P: no
    P->>P: declared sources, none resolve<br/>→ unknown provenance
    P-->>R: NEVER_INGEST
    R-->>A: count=0 · redacted=1
```

The rule in the "after" path is not new — `Enrichment.privacy_of` already
implements it and is pinned by a test. Task 1.1 is making one caller agree with
the other.

## 2. The Obsidian sync path — task 1.2

`views/` is safe **by accident**: it is regenerated filtered just before the copy.
Nothing regenerates `wiki/` or `raw/`.

```mermaid
flowchart TD
    SYNC["ProjectionService.integration_sync"]
    VIEWS["self.views(consumer=graph['consumer'])<br/><i>regenerates views/ filtered</i>"]
    IMPL["IntegrationService.sync(name, graph)"]

    SYNC --> VIEWS --> IMPL

    IMPL --> GJ["graph.json<br/><b>filtered</b> ✓"]
    IMPL --> VW["views/*.md<br/>safe by accident ✓"]
    IMPL --> WK["wiki/*.md<br/><b>rglob, unfiltered</b> ✗"]
    IMPL --> RW["raw/*  (include_raw)<br/><b>rglob, unfiltered</b> ✗"]

    WK --> LEAK["target dir<br/><i>usually iCloud / Dropbox</i>"]
    RW --> LEAK
    GJ --> LEAK
    VW --> LEAK

    FIX["<b>Task 1.2</b><br/>filter wiki/ by page privacy<br/>filter raw/ by record branch<br/>unknown provenance → exclude"]
    FIX -.->|"insert before _atomic_copy"| IMPL

    classDef bad fill:#fdecea,stroke:#a80f31,color:#14150f
    classDef ok fill:#e8f5ef,stroke:#16674a,color:#14150f
    classDef fix fill:#fff6e5,stroke:#8a6300,color:#14150f
    class WK,RW,LEAK bad
    class GJ,VW,VIEWS ok
    class FIX fix
```

## 3. The honest-status path — task 1.3

The shell already holds the correct answer and throws it away.

```mermaid
sequenceDiagram
    autonumber
    participant H as brainskit-status.sh
    participant BK as bk status --json
    participant PY as inline python renderer

    H->>BK: fetch (line 46)
    BK-->>H: {enforcement:{layers:[{layer,active,detail,script}...]}}
    Note over H: ✗ lines 83–96 IGNORE that and recompute<br/>[ -x gate.sh ] · [ -f .git/hooks/pre-commit ]
    H->>PY: argv[1]=write_gate argv[2]=commit_lint
    PY-->>H: "write gate active - commit lint active"
    Note right of PY: wrong under Husky, and wrong<br/>when hooks are unregistered

    Note over H,PY: AFTER — delete 83–96, drop the argv
    H->>PY: STATUS_JSON only
    PY->>PY: read status["enforcement"]["layers"]
    PY-->>H: each layer: active, else its own detail
```

## 4. Dependency direction — tasks 3.2, 3.3, 3.4

```mermaid
flowchart TD
    subgraph I["interfaces/"]
        CLI["cli.py · web.py · onboarding.py"]
    end
    subgraph AP["application/"]
        SVC["services · privacy · gate · health<br/>codegraph · projections · retrieval"]
        JS["jsonschema engine<br/><i>arrives from domain (3.2)</i>"]
        SENT["managed-block sentinel<br/><i>arrives from interfaces (3.3)</i>"]
    end
    subgraph D["domain/"]
        MOD["model.py<br/><i>after 3.2: zero third-party imports</i>"]
    end
    subgraph INF["infrastructure/"]
        VLT["vault · graph · integrations · codeanalysis"]
    end

    CLI --> SVC
    CLI --> SENT
    SVC --> MOD
    INF --> MOD
    INF -.->|"3.4: narrow ALLOWED<br/>graph.py:8 imports a function,<br/>not a port"| SVC

    classDef moved fill:#fff6e5,stroke:#8a6300,color:#14150f
    classDef clean fill:#e8f5ef,stroke:#16674a,color:#14150f
    class JS,SENT moved
    class MOD clean
```

## 5. The vendored tier — task 3.1

```mermaid
flowchart LR
    subgraph keep["Keep — 27 extractors, ~12,000 LOC, ~93% live"]
        EX["base extractors (8, no grammar)<br/>+ 11 behind code-all (4,008)"]
        CL["cluster.py — 320<br/><i>genuine delegation</i>"]
    end
    subgraph cut["Cut — task 3.1 then 4.5"]
        BV["build.py 1,643 + validate.py 95<br/><i>reached only for a 13-line helper</i>"]
        NX["networkx ≥ 3.4<br/><i>required → extra, or gone</i>"]
        DG["detect.py + google_workspace.py — 634"]
        SEC["security.py fetch stack — 211"]
    end
    NATIVE["<b>codegraph.py</b><br/>cycles (Tarjan) · diff (set difference)<br/>native, on code.json"]

    NATIVE --> CL
    NATIVE -.->|"replaces"| BV
    BV --> NX

    classDef keepc fill:#e8f5ef,stroke:#16674a,color:#14150f
    classDef cutc fill:#fdecea,stroke:#a80f31,color:#14150f
    class EX,CL,NATIVE keepc
    class BV,NX,DG,SEC cutc
```

## 6. File ownership — read before dispatching parallel agents

Tasks that share a file must go to **one** agent, or run in sequence. Collisions
below are real, not hypothetical.

| File | Tasks | Rule |
|---|---|---|
| `application/privacy.py` | **1.1, 2.3, 2.8** | One agent, in that order. 2.3 depends on 1.1's shape; 2.8 is a separate function but the same file. |
| `infrastructure/integrations.py` | **1.2, 2.10, 3.8** | Different phases — safe in sequence, never concurrent. |
| `application/codegraph.py` | **2.4, 3.1, 3.7** | 3.7 blocked by 2.4. 3.1 touches a different region; still one agent. |
| `interfaces/cli.py` | **1.4, 2.5, 3.3, 4.2, 4.3, 4.4** | The busiest file in the plan (3,337 LOC). Serialise by phase. |
| `tests/test_fix_services.py` | **1.1, 2.3, 2.8, 2.9** | Same agent as `privacy.py`, plus 2.9. |
| `tests/test_fix_integrations.py` | **1.2, 2.10, 3.8** | Follows `integrations.py`. |
| `tests/test_layering.py` | **3.2, 3.3, 3.4** | One agent for the whole layering slice. |
| `pyproject.toml` | **1.5, 3.1, 4.6** | Different keys, different phases. Never concurrent. |
| `.github/workflows/release.yml` | **1.5, 2.11, 4.6** | Sequential by definition. |

**Safe to parallelise within Phase 1:** `1.3` (shell template + `test_enforcement_status.py`)
and `1.4` (`gate.py` + `test_gate.py`) are disjoint from `1.1`/`1.2` and from each
other. `1.5` touches `pyproject.toml` and `release.yml` only.

**Contract edits belong to the orchestrator.** Where a task changes a signature two
other tasks code against — `strictest_privacy` in 2.3, `_read` in 2.4 — land that
signature change first, centrally, then dispatch against the fixed interface.

---
<!-- doc-tracking -->
- Created: 2026-08-12
- Updated: 2026-08-12 10:21
