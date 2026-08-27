# Repowise frontend deep-dive (source-level)

Repo: `repowise-dev/repowise` (default branch HEAD). Method: GitHub API tree + raw.githubusercontent.com fetches only — no clone. Every claim carries the file path it came from. The full repo is a monorepo: Python engine (`pyproject.toml`, `packages/cli`, `packages/server`, `packages/core`) + TS workspaces (`packages/{web,ui,types,api-client,vscode}`); the frontend is `packages/web` (Next.js app) + `packages/ui` (the component library where nearly all UI character lives).

---

## 1. Framework & libraries

**App**: Next.js ~15.5 App Router + React 19, TypeScript 5.7 — `packages/web/package.json` (`name: repowise-web`, "Next.js 15 dashboard and wiki viewer", `next ~15.5.21`, `react ^19`). `output: "standalone"`, `transpilePackages` for the three workspace packages, `optimizePackageImports: ["lucide-react","recharts"]`, images unoptimized — `packages/web/next.config.ts`.

**Component kit**: private library `@repowise-dev/ui` (`packages/ui/package.json`) — shadcn-style but hand-rolled: Radix primitives (`@radix-ui/react-{dialog,hover-card,label,popover,progress,scroll-area,select,separator,slider,slot,switch,tabs,tooltip}`) wrapped with `class-variance-authority` + `clsx` + `tailwind-merge` into `packages/ui/src/ui/{badge,button,card,confirm-dialog,dialog,input,label,popover,progress,scroll-area,select,separator,sheet,skeleton,slider,switch,tabs,tooltip}.tsx`. The public API is governed by `packages/ui/COMPONENT_CONTRACTS.md`: components are strictly presentational — canonical data in via props, UI-local state only, intent out via callbacks; injectable `LinkComponent` so the kit is framework-agnostic.

**Styling**: Tailwind CSS v4 (`@tailwindcss/postcss ^4`, `@tailwindcss/typography`) over a three-tier CSS-custom-property token system in one file, `packages/ui/styles/globals.css` (~everything; the app's `packages/web/src/styles/globals.css` is literally one line: `@import "@repowise-dev/ui/styles.css"`). Tailwind v4 `@theme` block + `@custom-variant dark (&:is(.dark *))` binding `dark:` to next-themes' `.dark` class.

**Graph/viz libraries** (`packages/ui/package.json`):
- `sigma ^3.0.3` + `@sigma/edge-curve` + `graphology ^0.26` + `graphology-layout-forceatlas2` + `graphology-layout-noverlap` — the main code graph (WebGL canvas)
- `@xyflow/react ^12.10` + `elkjs ^0.11` — the C4/architecture diagrams
- `d3-force`, `d3-hierarchy` (treemaps), `d3-shape` — bespoke charts
- `recharts` (^3.8 in ui, ^2.13 in web) — dashboard charts
- `mermaid ^11.16` — wiki diagrams
- `@tanstack/react-virtual` — big tables; `fuse.js` — fuzzy graph search
- **zero graph library** for the Zoom view — hand-written Canvas-2D renderer (see §5.2)

**Motion**: `framer-motion ^11` (page transitions, ring animation); `@lottiefiles/dotlottie-react` playing `packages/web/public/owl-loading.json` (loading owl mascot).

**Text/code**: `react-markdown ^10` + `remark-gfm`, `next-mdx-remote`, `shiki` (dual themes: github-light / vesper via `--shiki-light`/`--shiki-dark` vars — `packages/ui/styles/globals.css`), `sonner ^2` toasts, `cmdk ^1` command palette, `nuqs` (URL state), `swr ^2.2` (data), `zustand ^4.4` (C4/architecture stores), `next-themes ^0.4` (dark mode).

---

## 2. Design system (the actual tokens)

Source of truth: `packages/ui/styles/globals.css` (semantic tokens) + `packages/ui/src/brand.ts` (dependency-free canonical constants for OG images/email).

**Identity — "warm paper" light, neutral-black dark, orange accent** (`packages/ui/src/brand.ts`):
- accent `#f59520` ("Accent orange — the brand fill (CTAs, owl eyes, highlights)"), accent-on-light `#a16215`, plum `#473659`, ink `#241b2c`, paper `#fbf6f1`, cream `#fce9dd`

**Light theme** (`:root` in globals.css): bg-root `#fbf6f1` (warm paper), surface `#ffffff`, elevated `#fbf4ee`, inset `#f4eae1`; text `#241b2c` / `#5e5360` / `#8c7f88`; borders are *plum-tinted hairlines* `rgba(88,67,108,0.12)` (hover `0.22`); success `#1d8155`, warning `#9a6614`, error `#b23a2e`, info `#3f6ea5`.

**Dark theme** (`.dark` class, next-themes): bg-root `#0e0e0f`, surface `#141416`, elevated `#1c1c1f`, inset `#0a0a0b`; text `#f2f2f3`/`#b4b4b9`/`#7c7c82`; borders neutral white-alpha `rgba(255,255,255,0.07)`; accent stays `#f59520` full strength; success brightens to `#34d399`, error `#e06a5a`. Dark shadows carry **inset white hairlines**: `--shadow-md: 0 2px 8px rgba(0,0,0,0.45), inset 0 0 0 1px rgba(255,255,255,0.05)` — that's a lot of the "premium" depth feel. `color-scheme: light`/`dark` set per theme so native controls/scrollbars follow.

**Fonts** (`packages/web/src/app/layout.tsx` + globals.css): Geist Sans + Geist Mono (`geist` package) as product faces; **Lora (serif, next/font/google) for docs/wiki reading** — `--font-sans: "Geist","Inter",…`, `--font-mono: "Geist Mono","JetBrains Mono",…`, `--font-serif: var(--font-lora),"Lora",Georgia,serif`.

**Scales** (globals.css): text sizes are slightly *compact* — base `0.9375rem` (15px), xs `0.8125rem`, caption `0.625rem`, up to 3xl `2.25rem`; radii `6/8/10/14px` (sm/md/lg/xl); spacing `0.25rem`→`4rem`; layout constants `--sidebar-width: 260px`, `--context-panel-width: 280px`, `--content-max-width: 768px`; z-stack `base 0 → elevated 10 → dropdown 20 → sidebar 30 → modal 40 → command 50 → toast 60`; easing `--ease-out: cubic-bezier(0.16,1,0.3,1)`, durations `150/250/400ms`.

**Micro-label idiom** (seen in `packages/ui/src/chat/chat-interface.tsx` and across panels): `font-mono text-[10px] uppercase tracking-[0.12em]` — mono uppercase micro-labels are a recurring signature.

**Data-viz token families** (globals.css):
- 12 community hues (`--color-community-1..12` + `-soft` satellite variants), deliberately declared outside `@theme` so Tailwind v4 can't tree-shake them
- accent ramp `--color-ramp-1..5` built with `color-mix()` toward bg-inset (sequential scales)
- qualitative palette per refactoring type (extract-class `#b9651b`, extract-method `#0e7490`, move-method `#6d4aa6`, break-cycle `#b5396f`, split-file `#3f6ea5`…)
- language colors (Python `#3776ab`, TS `#3178c6`, Go `#00add8`, Rust `#dea584`…)
- edge-type colors (imports = accent, calls = success, inherits `#826aa0`, implements `#c85aa0`, co-change `#7c5cc4`)
- React Flow theming via `--xy-*` vars; Mermaid "blueprint look" (ink-plum node blocks `#241b2c` on a faint orange grid `rgba(245,149,32,0.10)`)
- Zoom canvas tokens: white floating cards, faint two-weight grid (`rgba(36,27,44,0.045)` minor / `0.08` major), paper-texture wash `url("/kg-card-paper.jpg")` behind knowledge-graph cards

**Chrome**: 6px transparent-track scrollbars with plum/white-alpha thumbs; focus ring `2px solid var(--color-accent-fill)`; `prefers-reduced-motion` globally collapses animations to 0.01ms; keyframes `graph-path-pulse` (orange glow pulse) and `graph-marching-ants` (dash offset) for path highlighting.

**Gradients** (brand.ts + globals.css): `--gradient-sunset: linear-gradient(135deg, #58436c 0%, #f59520 55%, #f7a94d 100%)` and warm/peach variants for heroes.

---

## 3. App shell & routing

**Shell** (`packages/web/src/app/layout.tsx`): html gets font variables; body gets tokens; provider stack `ThemeProvider → NuqsAdapter → SWRProvider → TooltipProvider`; then `ContextDrawerShell` wrapping `Sidebar` + `MobileNav` + `<main id="main-content">` (flex column so route-level bands subtract height rather than add); `CommandPalette` and `ThemedToaster` outside the main flow; sr-only skip link. Sidebar data (repos, workspace) is fetched server-side.

**Sidebar** (`packages/web/src/components/layout/sidebar.tsx`): logo header + global nav + collapsible workspace section + **repo list with expandable per-repo nav groups** (from `nav-items.ts` `repoNavGroups(repo.id)`) + footer (theme toggle, feedback, version). Collapses 260px → 56px icon rail; **auto-collapses on docs routes to give the reading column room, restores on exit**; active repo derived from `pathname.match(/^\/repos\/([^/]+)/)`; unindexed repos show a `needs index` pill; search button dispatches `window.dispatchEvent(new CustomEvent("repowise:open-command-palette"))` and renders a `⌘K` kbd.

**Routes** (`packages/web/src/app/**` tree — all verified paths):
- `/` dashboard (repo cards, active-job banner, quick actions, community summary grid)
- `/settings` (connection, display, provider/API-key, MCP tools, webhooks — `src/components/settings/*`)
- `/workspace` + `/workspace/{co-changes,conformance,contracts,system-map}` — cross-repo mode
- Per repo `/repos/[id]/…`: `overview`, `architecture`, `c4`, `graph`, `knowledge-graph`, `zoom`, `chat`, `docs` (+`docs/coverage`), `wiki/[...slug]`, `files` (+`files/[...path]`), `symbols` (+`[symbolId]`), `modules/[path]`, `commits`, `stats`, `owners` (+`[owner]`), `ownership`, `hotspots`, `coupling`, `code-health` (+`coverage`,`refactoring-targets`,`trend`; `health/*` aliases), `coverage`, `dead-code`, `risk`, `security`, `blast-radius`, `refactoring`, `decisions` (+`[decisionId]`), `costs`, `settings`
- Every section has `loading.tsx` skeletons and scoped `error.tsx`/`not-found.tsx`.

---

## 4. Data flow

- **REST via generated client**: `packages/web/src/lib/api/client.ts` calls `configureApiClient` from `@repowise-dev/api-client`; browser uses relative URLs (Next rewrites/`NEXT_PUBLIC_REPOWISE_API_URL`), server components hit `REPOWISE_API_URL` → fallback `http://localhost:7337` (the Python backend). API key from `localStorage["repowise_api_key"]` (set in settings) or env. ~35 typed endpoint modules under `packages/web/src/lib/api/` (graph, symbols, health, decisions, costs, jobs, chat, search, workspace…), consumed through SWR hooks in `src/lib/hooks/`.
- **Job progress via SSE**: `packages/web/src/lib/hooks/use-sse.ts` — plain `EventSource`, named events `progress`/`done`/`error`, message ring capped at 500 ("match the server's per-job ring buffer"), exponential backoff reconnect (`2^n * 1000ms`, max 3). Drives `live-job-progress.tsx` / `active-job-banner`.
- **Chat via POST + ReadableStream SSE**: `packages/web/src/lib/hooks/use-chat.ts` — `postChatMessage()` then `res.body.getReader()`, parses `data: {json}` lines; events `text_delta` (append), `tool_start` (tool call w/ "running" status), `tool_result` (status "done"), `done` (captures `conversation_id`/`message_id`), `error`; `AbortController` for cancel; conversation history reload by id.

---

## 5. Signature components

### 5.1 GraphFlow — the multi-mode code graph (`packages/ui/src/graph/graph-flow.tsx` + `graph/sigma/*`)
Despite the name, **not** xyflow — sigma.js on graphology. Node kinds `file`/`module`/`hub`/`core` with rich attributes (pagerank, betweenness, communityId, isHotspot, isDead, isEntryPoint…). Three layout modes in one component: **FA2 force** (`use-fa2-layout.ts`), **ELK hierarchical** (`use-elk-sigma-layout.ts`, chunked off the critical path above 1000 nodes), and a **precomputed radial "constellation"** (`radial-layout.ts` + `depth-rings.tsx` underlay: community hubs orbiting a repo core). Color modes `community | language | owner`; layered filters that share one "dim channel" (comment in source: *"the module filter and the community filter answer the same question — 'is this node outside what I asked for?' — so they share the one dim channel"*): Fuse fuzzy search (`use-graph-search.ts`), community toggles, module path-prefix dim, **ego-network depth filter** (`use-ego-filter.ts`), overlay signals `dead | hot | hideTests`. Companion panels: `graph-toolbar`, `graph-legend`, `path-finder-panel` (find route between two nodes; `graph-marching-ants` animates it), `graph-context-menu`, `graph-shortcut-help`, `centrality-leaderboard`, `graph-truncation-banner`. `SigmaCanvas` (`sigma/sigma-canvas.tsx`) exposes an imperative handle `focusNode/fitView/zoomIn/zoomOut`, right-click context menu, hover cursor swaps.

### 5.2 ZoomCanvas — the zero-dependency semantic-zoom code map (`packages/ui/src/zoom/`)
The `/repos/[id]/zoom` view. **Hand-written Canvas-2D renderer, no library**: `renderer.ts`, `scene.ts`, `camera.ts` + `camera-anim.ts` (fly-to), `cull.ts` (viewport culling), `geometry.ts`, `draw-tree.ts`, `paper.ts` (texture), `edges.ts`, `focus-path.ts`, `node-signals.ts`, `zoom-transition.ts`, `theme.ts` (resolves the `--color-zoom-*` tokens). `ZoomCanvas.tsx`: wheel zoom `Math.exp(-e.deltaY * WHEEL_ZOOM_RATE)` around cursor, drag pan with click-slop discrimination (`moved <= CLICK_SLOP_PX`), arrow-key pan 80px, `+`/`-` zoom 1.35×, double-click dives into a node, `pick(sx,sy)` hit-testing, hover card on `--color-bg-elevated`, `prefers-reduced-motion` respected. Visual: white "floating cards" on a faint two-weight grid — a Google-Maps-like city map of systems → layers → folders → files. Chrome: `zoom-breadcrumb`, `zoom-search`, `zoom-map-key`, `zoom-detail-panel`, `zoom-export-button` (web `src/components/zoom/`).

### 5.3 Command palette (`packages/web/src/components/search/command-palette.tsx`)
`cmdk` `Command.Dialog`; ⌘K + the custom `repowise:open-command-palette` event; groups: **Ask** (jump to chat), **Go to** (current repo's nav items), **Navigate**, **Workspace**, **Repositories**, **Files** (ranked: basename-match beats path-match — `(base.includes(q) ? 0 : 1000) + idx`, cap 12), **Pages** (semantic search via `useSearch`, limit 8). Backdrop `bg-black/60 backdrop-blur-sm`, sits at `z-[calc(var(--z-modal)+1)]`; an ownership check (`commandPaletteShortcutIsClaimed()`) prevents double-registration.

### 5.4 ChatInterface (`packages/ui/src/chat/chat-interface.tsx` + `chat/*`)
Single-column transcript on `--color-bg-root` with one chrome bar on `--color-bg-surface`; empty state = logo + "Ask anything about {repoName}" + hairline-separated suggestion chips; auto-growing textarea (`Math.min(ta.scrollHeight, 144)px`, Enter sends, Shift+Enter newline); streaming swaps Send → StopCircle with `onCancel`; **tool-call blocks** (`tool-call-block.tsx`, `tool-call-group.tsx`) render running/done tool invocations; **artifact panel** (`artifact-panel.tsx`) bottom-right with a pulse-badge counter when artifacts arrive while closed, dedup by type+title; **source citations** (`source-citation(s).tsx`) — "Every answer cites the pages it read", href-building injected via `buildCitationHref`; model selector and history are *slots* so the presentational core stays transport-agnostic.

### 5.5 DocsReader — the wiki reading experience (`packages/ui/src/docs/docs-reader.tsx` + `docs/*`)
Three-column: external tree nav | **720px max-width serif reading column** (`font-serif text-[2rem]` Lora h1; the file even documents the arithmetic: "chrome either side … is 56 + 288 + 300 = 644px, and body copy at 16px wants about 640px to reach 65 characters") | 2xl-only right rail with ToC, "intelligence" slot, related links, and a **generation receipt**: relative "updated …" time, page version, model name, input/output token counts. `WikiMarkdown` pipeline strips a duplicate leading h1, resolves `[[wiki-links]]` to real hrefs with in-app interception, and **filters content by reader persona** (`filterMarkdownByPersona`, `reader-persona.ts`). Mermaid + shiki dual-theme code blocks; `docs-mode-badge`, freshness/`drift-banner` (coverage components) surface staleness.

### 5.6 C4 architecture suite (`packages/ui/src/c4/`)
The second graph stack — `@xyflow/react` + `elkjs` (`layout/elk-c4-layout.ts`, `two-stage-layout.ts`, `edge-aggregation.ts`): C4 level tabs, typed nodes (`SystemNode`, `ContainerNode`, `ComponentNode`, `PersonNode`, `ExternalSystemNode`, `LayerClusterNode`, `PortalNode`…, all built on `node-shell.tsx`/`ink-node-shell.tsx`), panels (breadcrumb, legend, node inspector, `CodeViewer`, `FileExplorer`, `PathFinderModal`, `PersonaSelector`, guided-tour `LearnPanel`/`ArchTourButton`), overlays (`DiffOverlay`, `ExecutionFlowOverlay`), **export to PNG/SVG/JSON/Structurizr** (`export/*`), zustand stores, keyboard hook (`use-c4-keyboard.ts`), and a dedicated mobile layout (`mobile/MobileLayout.tsx`, `MobileBottomNav.tsx`).

### 5.7 Dashboard tile system (`packages/ui/src/dashboard/`)
`kpi-strip`, `health-score-ring` (SVG circle, `strokeDashoffset: circumference - progress`, framer 1.2s easeOut fill, thresholds 80/65/50/30 → Excellent…Critical), `language-donut`, `ownership-treemap` (d3-hierarchy), `dependency-heatmap`, `module-minimap`, `attention-panel`, `hotspots-mini`, `decisions-timeline`, `explore-cards`, `active-job-banner`. Same tile grammar reappears per-domain: costs (`cost-heatmap`, `roi-card`), git (`churn-vs-bus-factor-scatter`, `contributor-network`, `commit-category-sparkline`), health (`churn-complexity-quadrant`, `impact-effort-quadrant`, `risk-coverage-scatter`).

### 5.8 PresentOverlay — wiki-to-slides (`packages/ui/src/present/`)
Full-screen `fixed inset-0` overlay (`role="dialog" aria-modal`) that "escapes the dashboard chrome entirely… locks page scroll, theme-aware via CSS tokens only"; builds a deck **from wiki pages** (`build-present-model.ts`, `split-markdown.ts`) with two modes — slide deck (`deck-view.tsx`) and walkthrough (`walkthrough-view.tsx`) — separate indices preserved when toggling; arrows/home/end/escape via `use-present-keyboard.ts`; `slide-progress.tsx`.

### 5.9 Page transitions + toaster + theming glue (`packages/web/src/components/layout/`)
`page-transition.tsx`: framer `AnimatePresence` keyed on `usePathname()` — `initial {opacity:0, y:6} → animate {opacity:1, y:0} → exit {opacity:0, y:-4}`, `0.15s easeOut` (subtle rise-in on every route change). `themed-toaster.tsx` (sonner on tokens), `theme-provider.tsx` (next-themes), `whats-new-modal`, `upgrade-banner`, `reindex-hint-banner`, `context-drawer-provider` (right-side context drawer shell).

### 5.10 Blast radius & dead code storytelling views (`packages/ui/src/blast-radius/`, `dead-code/`)
Opinionated "lede + evidence tables" pages: `risk-score-card`, direct/transitive/cochange/reviewers tables, `test-gaps-list`, `impact-graph`; dead-code has `safe-to-delete-pile` and `owner-leaderboard`. Pattern: every analysis page opens with a narrative lede component (`*-lede.tsx` exists in commits, dead-code, health, coverage, refactoring) — prose summary first, tables second.

---

## 6. Interactions summary

- **Keyboard**: ⌘K palette; graph shortcut help panel (`graph-shortcut-help.tsx`) + `use-graph-keyboard-shortcuts.ts`; C4 keyboard hook; zoom canvas arrows/+/-; present overlay arrows/esc; Enter/Shift+Enter composer.
- **Live updates**: EventSource SSE for jobs (progress/done/error + backoff), fetch-stream SSE for chat (text_delta/tool_start/tool_result/done), pulse badge for artifacts, active-job banner.
- **Motion**: 150ms route fade/rise; 1.2s ring fill; marching ants + path pulse in graphs; camera fly-to in zoom/sigma; Lottie owl while loading; global reduced-motion kill switch.
- **URL state**: `nuqs` for filters; drawers/sheets (Radix + `sheet.tsx`) for file/symbol/health detail; right context drawer (280px) as secondary inspector.

---

## 7. Portability mapping → brainkit web SPA (`src/brainskit/interfaces/web.py`)

Brainkit constraint restated: single-file dark SPA, stdlib-served, no build/npm, vendored three.js 3D force graph, five views + chat + capture modal + toasts, JSON APIs (FTS5 search, graph, code-graph, proposals, timeline, sources, pages, status).

**Directly portable as inline HTML/CSS/vanilla JS (no doctrine change):**

| Repowise pattern | How it lands on brainkit |
|---|---|
| **Token system** (3-tier CSS vars, semantic surfaces, plum-hairline borders, inset-white-hairline dark shadows, z-stack, micro-label idiom, 6px scrollbars, focus ring, reduced-motion) | Pure CSS — drop-in. Brainkit is dark-only; adopt the dark tier (`#0e0e0f/#141416/#1c1c1f`, white-alpha borders, one saturated accent at full strength, `inset 0 0 0 1px rgba(255,255,255,0.05)` card hairlines). The mono `10px` uppercase `0.12em` micro-label is free character. |
| **ZoomCanvas** (§5.2) | **The best-fit signature piece**: it is already zero-dependency Canvas-2D with hand-rolled camera/cull/hit-test — exactly the shape brainkit code has to be. Reimplement `wheel exp-zoom around cursor + click-slop + double-click dive + fly-to` in vanilla JS over the existing graph JSON (communities → cards → pages/sources as semantic levels). |
| **Command palette** | cmdk is React-only, but the *behavior* is small: ⌘K overlay + grouped list (Views / Actions / FTS5 results / recent) + ranked file matching (`basename hit beats path hit`). Vanilla dialog + `fetch('/api/search')` with debounce. Brainkit already has FTS5 search API — this is the highest leverage/lowest cost port. |
| **Chat event grammar** | Brainkit chat can adopt the exact stream contract: POST + `ReadableStream` reader parsing `data:` lines with `text_delta / tool_start / tool_result / done / error`, AbortController cancel, Send↔Stop button swap, suggestion chips, and "every answer cites the pages it read" — citations map 1:1 to brainkit's `[^source:<sha256>]` provenance. Vanilla JS, no deps. |
| **DocsReader ergonomics** | Pages view: 720px column, serif for reading (system serif stack — Georgia/Iowan — instead of shipping Lora), right-rail ToC built by walking rendered headings, and the **generation receipt** (updated-relative-time, version, model, token counts) which maps directly onto brainkit's freshness ledger + apply metadata. All CSS/vanilla. |
| **Sidebar grammar** | 260px→56px icon-rail collapse, per-section expandable groups, `needs index`-style status pills (→ pending/stale badges), auto-collapse on reading views. CSS + a few event listeners. |
| **Health ring & tiles** | SVG `stroke-dashoffset` ring with CSS transition (framer not needed), KPI strip, hairline-separated "lede + tables" analysis pages. Hand-rolled SVG; no chart lib. |
| **Present overlay** | Fixed-inset overlay that turns wiki pages into a keyboard-driven deck (`split markdown on h2`) — vanilla, and a genuinely novel feature for a vault (present a synthesis page). |
| **Page transition** | 150ms opacity/y CSS transition on view switch (brainkit swaps views in-page — even easier than route-keyed AnimatePresence). Honor `prefers-reduced-motion`. |
| **SSE job progress** | `EventSource` + named events + capped ring + exponential backoff — vanilla by definition; would suit long `bk` operations (ingest batches, code build) if the stdlib server adds an SSE endpoint (chunked response is doable in stdlib http.server). |

**Portable only by vendoring a prebuilt bundle (doctrine-compatible, same precedent as vendored three.js):**
- **sigma.js + graphology** (MIT): would enable the 2D constellation/FA2 view. But brainkit already has a three.js force graph — the higher-value port is the *interaction layer*, which is library-independent logic: ego-depth filter, community/module filters sharing one dim channel, color modes (community/language|branch/owner), legend, path-finder with marching-ants, centrality leaderboard. All expressible against the existing 3D graph.
- **mermaid** (MIT, ~2MB) — heavy; probably skip.

**Requires abandoning the no-build doctrine (do not port):**
- React 19/Next/App Router itself, Radix primitives, cmdk, recharts, framer-motion, @xyflow/react + elkjs (the C4 suite is deeply React-shaped), shiki (Next server-renders it), next-themes, SWR/zustand/nuqs, dotlottie. Everything above already lists the vanilla equivalent of what matters.

**License gate — AGPL-3.0** (`LICENSE`): repowise is AGPL-3.0, including `packages/ui`. **Copying code or CSS verbatim into brainkit would make brainkit an AGPL derivative** (with the network-service source-provision clause). The safe port is what this report enables: reimplementation of *patterns, layouts, interaction grammars and the token architecture* from description; individual hex values/scales are unprotectable facts, but do not paste the globals.css blocks wholesale. Do not copy assets: `packages/web/public/kg-card-paper.jpg` (paper texture) and `owl-loading.json` (Lottie mascot) are repo assets of unverified origin/authorship. UI-code third-party deps themselves (Radix/cmdk/sigma/graphology/elkjs/recharts/framer/fuse/mermaid) are permissive (MIT/Apache-class) and irrelevant if not vendored; fonts Geist and Lora are SIL OFL (usable independently — flagged Unverified below).

---

## 8. Unverified

- Font licenses (Geist = SIL OFL 1.1 per Vercel, Lora = SIL OFL via Google Fonts) — from memory, not fetched from this repo.
- `packages/ui/src` tree was truncated after `shared/api-error.tsx` in the API response; the remaining directories (`shared/`, `stats/`, `symbols/`, `ui/`, `wiki/`, `workspace/`, `zoom/`) were enumerated via targeted contents calls (`ui/`, `zoom/`) and the COMPONENT_CONTRACTS inventory (`wiki/`, `shared/`, `symbols/`, `workspace/` component names) — individual files in those last four dirs not all listed here.
- `useSigmaRenderer` internals (`use-sigma.ts` — node/edge programs, label thresholds) not fetched; renderer settings described only at the level visible from `sigma-canvas.tsx`.
- recharts version skew (web ^2.13 vs ui ^3.8) observed in the two package.json files; how it's deduped not investigated.
- Backend chat/search endpoint shapes are inferred from the client hooks, not from `packages/server` source (sibling agent's territory).
- WebFetch summaries are model-condensed; exact values quoted (hex, cubic-beziers, sizes) came through verbatim quoting instructions, but any un-quoted phrasing is paraphrase.
