<!-- Stage 03. Architecture diagrams for the remediation programme. -->

# Architecture — where the fail-open pattern lives

> Written as Mermaid rather than `.excalidraw`: these are dependency and
> decision-flow diagrams, which Mermaid renders losslessly and reviewably in a
> diff. Use `/diagram architecture` if a hand-editable Excalidraw canvas is
> wanted for a presentation.

## 1. The one root cause, five surfaces

Every P0/P1 finding is the same mistake at a different altitude: an unknown
resolved permissively, or an existence check standing in for a behaviour check.

```mermaid
flowchart TD
    ROOT["<b>Root cause</b><br/>unknown → permissive answer<br/>existence check ≠ behaviour check"]

    ROOT --> S1["<b>A1</b> _evidence_privacy<br/>unresolvable hashes dropped<br/>empty set → CLOUD"]
    ROOT --> S2["<b>A2</b> Obsidian sync<br/>filters the graph object,<br/>rglob's wiki/ + raw/"]
    ROOT --> S3["<b>B1</b> brainskit-status.sh<br/>[ -x gate.sh ] → 'active'<br/>[ -f pre-commit ] → 'active'"]
    ROOT --> S4["<b>B4</b> code-graph _read()<br/>returns disk artifact,<br/>never consults staleness()"]
    ROOT --> S5["<b>A4</b> sourced_from unknown hash<br/>graph.py drops silently,<br/>health.py lints it"]

    S1 --> L1(["never-ingest served to cloud<br/>stamped privacy: cloud"])
    S2 --> L2(["secret written to iCloud/Dropbox<br/>under default options"])
    S3 --> L3(["every agent session opens<br/>with a false enforcement claim"])
    S4 --> L4(["hubs cites deleted files<br/>with exact line numbers"])
    S5 --> L5(["two code paths,<br/>two answers about one fact"])

    CORRECT["<b>The rule already written</b><br/>Enrichment.privacy_of<br/>no resolving sources → NEVER_INGEST<br/><i>pinned by a test</i>"]
    CORRECT -.->|"mirror this"| S1
    CORRECT -.->|"and extend to"| S2

    classDef bug fill:#fdecea,stroke:#a80f31,color:#14150f
    classDef leak fill:#fff6e5,stroke:#8a6300,color:#14150f
    classDef good fill:#e8f5ef,stroke:#16674a,color:#14150f
    classDef root fill:#14150f,stroke:#14150f,color:#ffffff
    class ROOT root
    class S1,S2,S3,S4,S5 bug
    class L1,L2,L3,L4,L5 leak
    class CORRECT good
```

## 2. Blast radius of A1 — one helper, every consumer-scoped read

`_evidence_privacy` is shared. Fixing it once closes every path below; leaving it
open means each path is independently wrong.

```mermaid
flowchart LR
    EP["_evidence_privacy<br/><i>application/privacy.py</i>"]

    EP --> C1["bk search"]
    EP --> C2["bk context"]
    EP --> C3["read_resource<br/><i>serves full page body</i>"]
    EP --> C4["browse_pages"]
    EP --> C5["graph_data"]
    EP --> C6["bk export"]
    EP --> C7["Neo4j / Postgres<br/>integrations"]
    EP --> C8["web viewer"]

    classDef hub fill:#fdecea,stroke:#a80f31,color:#14150f,font-weight:bold
    classDef sink fill:#fbfbfa,stroke:#c2c2bb,color:#14150f
    class EP hub
    class C1,C2,C3,C4,C5,C6,C7,C8 sink
```

## 3. Track F — the tier above the vendored tree

The extractors are healthy and stay. The cost is the *analysis tier* importing
a 1,643-line module at load time to reach a 13-line helper.

```mermaid
flowchart TD
    subgraph now["Today"]
        direction TB
        CG1["application/codegraph.py<br/>cycles · diff · communities"]
        AN1["codeanalysis/analyze.py:6<br/><code>from graphify.build import edge_data</code>"]
        BU1["build.py — 1,643 LOC"]
        VA1["validate.py — 95 LOC"]
        NX1["networkx ≥ 3.4<br/><i>hard runtime pin</i>"]
        ED1["edge_data — <b>13 lines</b>"]

        CG1 --> AN1 --> BU1
        BU1 --> VA1
        BU1 --> NX1
        BU1 --> ED1
    end

    subgraph after["After F1"]
        direction TB
        CG2["application/codegraph.py<br/>cycles (Tarjan) · diff (set difference)<br/><i>native, on code.json</i>"]
        CL2["codeanalysis/cluster.py — 320 LOC<br/><i>the only genuine delegation</i>"]
        CG2 --> CL2
    end

    now -.->|"−1,738 LOC · networkx pin dropped"| after

    classDef fat fill:#fdecea,stroke:#a80f31,color:#14150f
    classDef lean fill:#e8f5ef,stroke:#16674a,color:#14150f
    class BU1,VA1,NX1,AN1 fat
    class CG2,CL2 lean
```

## 4. Track F3 — the triplicated installer contract

The layering rule is right; the response to it was to copy rather than move.
Writer and readers can silently disagree about what "installed" means — and per
this repo's history, that divergence has already shipped twice.

```mermaid
flowchart TD
    subgraph i["interfaces/"]
        CLI["cli.py — 3,337 LOC<br/><i>625-line agent installer</i><br/>gate constants ·  sentinel · mechanism strings"]
    end
    subgraph a["application/"]
        HE["health.py:688–693<br/><i>copies</i> the sentinel<br/><small>'the application layer must<br/>not depend on interfaces'</small>"]
        GA["gate.py<br/><b>already owns</b><br/>the gate constants"]
    end

    CLI -. "duplicated, not imported" .-> HE
    GA -. "should be imported<br/><b>two-line fix</b>" .-> CLI
    HE -->|"sentinel moves DOWN<br/>into application/"| GA

    classDef dup fill:#fff6e5,stroke:#8a6300,color:#14150f
    classDef ok fill:#e8f5ef,stroke:#16674a,color:#14150f
    class CLI,HE dup
    class GA ok
```

## 5. Phase gates

```mermaid
flowchart LR
    P1["<b>Phase 1</b><br/>A1 A2 B1 B2 C1<br/><i>stop the leak</i>"]
    P2["<b>Phase 2</b><br/>A3 A4 A5 B4 D1 D2 D3<br/>E1 E2 E3 C2<br/><i>honest answers + way in</i>"]
    P3["<b>Phase 3</b><br/>F1 F2 F3 F4 B3 E4 E5 G2<br/><i>architecture</i>"]
    P4["<b>Phase 4</b><br/>G1 G3 G4 F5 C3 C4<br/><i>docs truth + release</i>"]
    REL(["brainskit 0.6.0<br/>on PyPI"])

    P1 --> GATE1{{"suite green · ruff clean<br/>bk lint clean<br/><b>negative controls shown</b>"}} --> P2
    P2 --> GATE2{{"same gate"}} --> P3
    P3 --> GATE3{{"same gate"}} --> P4
    P4 --> GATE4{{"same gate<br/>+ version single-sourced"}} --> REL

    classDef phase fill:#fbfbfa,stroke:#14150f,color:#14150f
    classDef gate fill:#f1f1ef,stroke:#d9421c,color:#14150f
    classDef rel fill:#e8f5ef,stroke:#16674a,color:#14150f
    class P1,P2,P3,P4 phase
    class GATE1,GATE2,GATE3,GATE4 gate
    class REL rel
```

---
<!-- doc-tracking -->
- Created: 2026-08-12
- Updated: 2026-08-12 10:07
- Updated: 2026-08-12 10:08
