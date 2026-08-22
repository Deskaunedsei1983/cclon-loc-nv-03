# AI-Stack — Architektur & Funktionen im Detail

> Stand: 2026-06 · Branch `claude/gifted-carson-viw85x`
> Begleitdokumente: [`FLOW.md`](./FLOW.md) (Abläufe) · [`USECASES.md`](./USECASES.md) (Use-Cases)

Ein **komplett selbst-enthaltenes**, lokales AI-Bundle nahe an claude.ai
(Code-Ausführung, Office-Dateien, RAG, Web-Gegenprüfung, Gedächtnis) bei
**100 % lokaler Inferenz**, mit **DSGVO-Härtung** und **VRAM-Bewusstsein**.

---

## 1. Designprinzipien

| Prinzip | Umsetzung |
|---|---|
| **Lokal & DSGVO** | Inferenz lokal; Such-Queries PII-maskiert (Presidio); Code-Sandbox ohne Egress; externer Egress nur in isolierten Single-Purpose-Sidecars |
| **VRAM-bewusst** | Quantisierung (NVFP4-Gewichte, FP8-KV); genau **ein** Hauptmodell (Profil); optionaler GPU-Helfer; **CPU**-Helfer für JSON-Tasks; serielles Modell-Laden gegen OOM |
| **Stabile Schnittstellen** | Hauptmodell immer unter Alias `vllm-main:5568` / Modell-ID `main` → Agent/OWUI/RAGFlow bleiben bei jedem Modellwechsel unverändert |
| **Ehrlichkeit vor Schein** | Agent verankert das echte Datum, quantifiziert die Beleglage (RAG %/Web %) und kennzeichnet Unbelegtes statt es als gesichert auszugeben |

---

## 2. Netze

| Netz | Typ | Egress | Zweck |
|---|---|---|---|
| `aistack-core` | bridge | ja | Backbone: Inferenz, Embedder, Qdrant, Suche, UI, Agent |
| `aistack-sandbox` | bridge `internal` | **nein** | Code-Sandbox — generierter Code kann nichts exfiltrieren |
| `aistack-rag` | external | — | schmale Brücke Agent ⇄ RAGFlow |
| (default) | bridge | ja | **nur** der `blocklist-fetcher`-Sidecar — hängt an keinem internen Netz, sieht keine PII |

---

## 3. Dienste-Inventar

| Dienst | Image | Port (Host) | Profil | Funktion |
|---|---|---|---|---|
| `vllm-main-nemotron` | vllm-openai | 5568 | `main-nemotron` | **Haupt-LLM (empfohlen)** Nemotron-3.5-Lightning-30B-A3B-NVFP4 **+ DSpark** |
| `vllm-main` | vllm-openai | 5568 | `main-qwen` | Haupt-LLM Qwen3.6-35B-A3B-NVFP4 **+ MTP** |
| `vllm-main-qwen-plain` | vllm-openai | 5568 | `main-qwen-plain` | dasselbe **ohne** MTP (sauberes Structured Output) |
| `vllm-helper` | vllm-openai | 30001 | `helper` | kleines Task-LLM Qwen3.5-4B (**optional**) |
| `mem0-struct` | llama.cpp:server | 8088 | `mem0struct` | **CPU** JSON-/Task-Helfer (Qwen2.5-3B-Instruct GGUF) |
| `embeddings` | text-embeddings-inference | 8082 | — | TEI **bge-m3** (1024 Dim) → **nur Mem0** |
| `vllm-embed` | vllm-openai | 8091 | — | Qwen3-Embedding-4B (2560 Dim) → RAGFlow/Morphik |
| `qdrant` | qdrant | 6333 | — | Mem0-Vektorstore (on-disk) |
| `searxng` | searxng | (intern) | — | Meta-Suche (7 Engines) |
| `presidio-proxy` | presidio-search-proxy | 8080 | — | **PII-Maskierung** der Such-Queries |
| `browserless` | browserless/chromium | (intern) | — | Headless-Chromium für tiefe Websuche |
| `code-sandbox` | code-sandbox | (intern) | — | luftdichte Code-Ausführung + Jupyter |
| `agent` | ai-agent | 9009 | — | **research-agent** (Orchestrierung) |
| `ingest-router` | ingest-router | 9010 | — | Upload-Weiche → Morphik/RAGFlow |
| `open-webui` | open-webui | 3009 | — | Frontend |
| `blocklist-fetcher` | curl | — | `blocklist` | isolierter Egress-Sidecar (Domain-Blocklist) |
| `morphik` (+pg/redis) | morphik-core | 8083 | `morphik` | visuelles/multimodales RAG (ColPali) |
| `open-computer-use` | open-computer-use | 8084 | `computer-use` | Computer-Use-Agent |
| `fragments` | fragments | 3010 | `fragments` | Artifact-/Fragment-UI |
| `microsandbox-executor` | — | — | `microvm` | microVM-Code-Executor (Host) |
| RAGFlow (`./ragflow`) | ragflow + ES/MySQL/MinIO/Redis | 9380, 80 | (eigenes Bundle) | Text-RAG |
| Observability | Grafana/Loki/Dozzle/Prometheus/Netdata | 3011, 8085, 9090, 19999 | (eigenes Overlay) | zentrales Logging/Metriken |

---

## 4. Inferenz-Schicht

### 4.1 Hauptmodell — drei Varianten, genau **eine** aktiv
Auswahl per `COMPOSE_PROFILES` (genau ein `main-*`-Profil; `start.sh` erzwingt das).
Alle drei hängen am **Alias `vllm-main:5568`** und liefern die ID **`main`**.

| Profil | Modell | Besonderheit | Gut für |
|---|---|---|---|
| `main-nemotron` | Nemotron-3.5-Lightning-30B-A3B-NVFP4 | hybrides **Mamba-MoE** (30B/3B aktiv), **DSpark**-Speculative-Decoding (3 Tokens, eigener Draft-Checkpoint), `--moe-backend marlin`, `--mamba-backend flashinfer`, `--reasoning-parser nemotron_v3`, Tools `qwen3_xml`, 260k (Modell kann 1M), KV fp8_e4m3 | **Default** — braucht vLLM ≥ 0.27.1 |
| `main-qwen` | Qwen3.6-35B-A3B-NVFP4 | **MTP** Speculative Decoding, `--reasoning-parser qwen3`, Tools `qwen3_coder`, 256k, KV fp8 | schnellstes Chat |
| `main-qwen-plain` | dito | **ohne** MTP | **sauberes Structured Output** (mem0 `infer=true`) — MTP × guided decoding erzeugt sonst kaputtes JSON |

### 4.2 GPU-Helfer (`helper`, optional)
`vllm-helper` (Qwen3.5-4B, `:30001`, ID `qwen-helper`). Klein. Im Default **aus**
(VRAM frei). War früher OWUIs Task-Modell — diese Rolle hat jetzt der CPU-Helfer.

### 4.3 Zwei Embedder (bewusst getrennt)
- **`embeddings`** = TEI **bge-m3** (1024 Dim) → ausschließlich **Mem0**.
- **`vllm-embed`** = **Qwen3-Embedding-4B** (2560 Dim; 8B=4096 als Option) → **RAGFlow & Morphik-Text**.
  Steuerung: `EMBED_MODEL`, `EMBED_GPU_UTIL`. Dimensionswechsel ⇒ RAGFlow-KB neu **und** `morphik.toml`/`VECTOR_DIMENSIONS` angleichen.

### 4.4 CPU-Struct-Helfer (`mem0struct`)
`mem0-struct` = **llama.cpp-Server** mit **Qwen2.5-3B-Instruct** (Nicht-Reasoning),
`:8088`, Alias/ID `mem0-struct`, **~0 VRAM**. Liefert garantiert valides JSON
(`--reasoning off` + GBNF unter `response_format`) und bedient **zwei** Rollen:
mem0s JSON-Bedarf **und** OWUIs Task-Modell (Titel/Tags/Suchqueries). Modell frei
wählbar via `MEM0_STRUCT_HF`. **Wichtig:** Reasoning-Modelle (Qwen3.x) sind hier
ungeeignet — bei aktivem Thinking greift die JSON-Grammatik nicht (llama.cpp #20345).

---

## 5. Gedächtnis — Mem0 + Qdrant

- **Mem0** speichert Fakten in **Qdrant** (on-disk, `:6333`, Collection `mem0_main`),
  Embedder **bge-m3**. Der Agent sucht zu Beginn relevante Erinnerungen und schreibt
  am Ende neue.
- **Adaptiver LLM-Selektor** (`_pick_mem0_llm`, `MEM0_LLM_BASE_URL=auto`):
  `mem0-struct (CPU)` → `vllm-helper` → aktives Hauptmodell (Modelle ohne garantiertes JSON
  wird an den served-model-names erkannt und **übersprungen**) → sonst **leise aus**.
- **`MEM0_INFER`** (Default **false**):
  - `false` = **robustes Direkt-Speichern** der User-Aussagen (kein zweiter LLM-Call;
    deterministisch). mem0s zweistufiger „Memory-Manager" ist mit kleinen lokalen
    Modellen fragil (leeres JSON → `Expecting value`).
  - `true` = destillierte Fakten — **braucht** ein zuverlässiges JSON-LLM (z. B.
    `main-qwen-plain`, **nicht** das MTP-Qwen).
- **`MEM0_ENABLED`** schaltet Memory hart ab.

---

## 6. Steuerung — research-agent

OpenAI-kompatibler Dienst (`:9009`, ID **`research-agent`**). Zwei Varianten via
`AGENT_IMPL`:
- **`pydantic`** (Default): klassisches Tool-Calling (RAG/Web/Code) via PydanticAI.
- **`langgraph`**: Critic-Loop **mit deterministischer Web-Gegenprüfung**.

### 6.1 LangGraph-Pipeline
```
gather → draft → execute → verify → critic → (REVISE ↺ draft | APPROVE → Ende)
```
- **gather** — Retrieval aus RAGFlow (+ Morphik, falls aktiv).
- **draft** — LLM erzeugt belegten Entwurf. Eigener Orchestrierungs-Prompt: **keine**
  Tool-Calls (Retrieval/Web/Code macht der Graph), optional **ein** ` ```python `-Block.
- **execute** — Code in der luftdichten Sandbox.
- **verify** — **deterministische** Web-Gegenprüfung: PII-freie Pruef-Queries → `search_web`
  → quantifizierte Quoten (siehe 6.3).
- **critic** — datums-bewusst; bewertet Belegtreue/Einarbeitung → `APPROVE`|`REVISE`.

### 6.2 Datums-Anker
`now_context()` injiziert **pro Anfrage** das reale Datum/Uhrzeit (TZ `Europe/Vienna`,
UTC-Fallback). Verhindert „X hat noch nicht stattgefunden"-Halluzinationen; auch in
beide verify-Schritte und den Critic eingespeist.

### 6.3 Quantifizierte, gewichtete Web-Gegenprüfung
Pro prüfbarer Aussage:
```
<Aussage>
  RAG: p% (k/n Belege | Dok)
  Web: q% (j/m Quellen, davon t vertrauenswuerdig | Domains)
  Fazit: [BESTAETIGT] | [AKTUELLER] | [WIDERSPRUCH] | [KEINE FUNDE]
```
- `[KEINE FUNDE]` (0 %/0 %) = **kein Fehler**, nur kein Beleg → Aussage ruht transparent
  auf Modellwissen.
- **Trust-Liste** `agent/trust_domains.txt`: Format `domain [tier] [topics]`,
  Wildcard `*.xy.com`, Themen (z. B. `recht`). Vertrauenswürdige Quellen zählen STARK,
  themen-passend „voll". Schnell-Ergänzung per `.env` `TRUST_DOMAINS`/`LOW_TRUST_DOMAINS`.
- **Auto-Blocklist** (low-Tier): der isolierte `blocklist-fetcher`-Sidecar zieht eine
  externe Domainliste in ein Volume; der Agent **liest** nur die Datei → **kein
  Agent-Egress** (`BLOCKLIST_URL` aktiviert, Profil `blocklist`).

---

## 7. Retrieval

- **RAGFlow** (`./ragflow`, eigenes Sub-Bundle/Netz): Text-RAG (`:9380` API, `:80` UI),
  Embedder `qwen3-embed`. ES/MySQL/MinIO/Redis bleiben gekapselt.
- **Morphik** (`morphik`, optional): **visuelles/multimodales** RAG (ColPali
  `tsystems/colqwen2.5-3b-multilingual`, lokal auf der GPU), `:8083`, pgvector + Redis.
  Embedding-Dimension (`VECTOR_DIMENSIONS`/`morphik.toml`) **muss** zur `vllm-embed`-Dim passen.
- **ingest-router** (`:9010`): klassifiziert Uploads → **Morphik** (Bild/Scan/Tabelle) |
  **RAGFlow** (Text).

---

## 8. Suche & PII-Pfad

```
Agent/OWUI ─► presidio-proxy (maskiert PII) ─► searxng (7 Engines) ─► Web
                                                       │
OWUI-Treffer ─► browserless (Headless-Chromium, rendert JS) ─► echte Inhalte
```
Engines: `bing, brave, duckduckgo, mojeek, qwant, wikipedia, wikidata` (Wikipedia/-data
als „Ergebnis-Boden", blocken selten). Diese Liste **nicht** verkleinern.

---

## 9. Ausführung

- **code-sandbox** (Netz `aistack-sandbox`, `internal` → **kein Egress**): `/run` +
  Jupyter, Office-Libs (python-docx/openpyxl/python-pptx/nbformat, polars/duckdb).
  Generierter Code kann nichts ins Netz schicken.
- **microsandbox-executor** (optional, `microvm`): microVM-Ausführung auf dem Host
  statt Subprozess.

---

## 10. Frontend — Open WebUI (`:3009`)

- **Connections**: nur **`research-agent`** (Agent) + **`mem0-struct`** (Task-Modell).
  Das rohe Hauptmodell ist **nicht** verbunden → kein verwirrendes Doppel-Modell im Menü
  (der Agent spricht `vllm-main` intern).
- **Task-Modell** (Titel/Tags/Suchqueries/Folgefragen) = **`mem0-struct`** (CPU, ~0 VRAM).
  Schalter `OWUI_OPENAI_API_BASE_URLS`, `OWUI_TASK_MODEL`, `OWUI_ENABLE_TASKS`.
- `ENABLE_PERSISTENT_CONFIG=False` → die `.env`/Compose ist **Single Source of Truth**
  (keine UI-Klick-Settings nötig).
- Websuche: searxng (über Presidio) + browserless.

---

## 11. Observability (Default an, `LOGGING_STACK=0` zum Abschalten)

Grafana (`:3011`), Loki + Promtail (Log-Aggregation), Dozzle (`:8085`, Live-Logs),
Prometheus (`:9090`), Netdata (`:19999`), node-exporter, cadvisor, nvidia-exporter.
Dashboard „AI-Stack — Container-Logs & Fehler" zeigt u. a. den Websuche-Pfad.

---

## 12. Profile-Matrix

| `COMPOSE_PROFILES`-Wert | startet |
|---|---|
| `main-nemotron` / `main-qwen` / `main-qwen-plain` | das jeweilige Hauptmodell (genau eines) |
| `helper` | GPU-Task-Helfer (optional) |
| `mem0struct` | CPU-Struct-Helfer (mem0 + OWUI-Tasks) — **empfohlen** |
| `blocklist` | Auto-Blocklist-Sidecar |
| `morphik` | visuelles RAG |
| `computer-use` | Computer-Use-Agent (+ Postgres) |
| `fragments` | Artifact-UI |
| `microvm` | microVM-Executor |

Beispiel-Default: `COMPOSE_PROFILES=main-qwen,mem0struct`.

---

## 13. Wichtige `.env`-Schalter (Auszug)

| Schalter | Default | Wirkung |
|---|---|---|
| `COMPOSE_PROFILES` | `main-qwen,mem0struct` | aktive Profile (genau **ein** `main-*`) |
| `VLLM_IMAGE` | `vllm/vllm-openai:nightly` | gepinntes vLLM-Image |
| `EMBED_MODEL` / `EMBED_GPU_UTIL` | `Qwen/Qwen3-Embedding-4B` / `0.15` | RAGFlow/Morphik-Embedder |
| `MEM0_ENABLED` | `true` | Memory an/aus |
| `MEM0_INFER` | `false` | `false`=Direkt-Speichern · `true`=destillierte Fakten |
| `MEM0_LLM_BASE_URL` | `auto` | Selektor (struct→helper→main) oder feste URL |
| `MEM0_STRUCT_HF` | `Qwen/Qwen2.5-3B-Instruct-GGUF:Q4_K_M` | Modell des CPU-Helfers (Nicht-Reasoning!) |
| `OWUI_TASK_MODEL` / `OWUI_ENABLE_TASKS` | `mem0-struct` / `true` | OWUI-Hintergrundaufgaben |
| `TRUST_DOMAINS` / `LOW_TRUST_DOMAINS` | leer | Trust-Liste ergänzen |
| `BLOCKLIST_URL` | leer | Auto-Blocklist aktivieren (+ Profil `blocklist`) |
| `SERIAL_GPU_LOAD` | `1` | serielles GPU-Laden gegen OOM |
| `AGENT_IMPL` | `pydantic` | `pydantic` \| `langgraph` |

---

## 14. Start / Stop

- **`./start.sh [overlay-profile…]`**: stellt Netz sicher → startet RAGFlow → lädt die
  GPU-Modelle **seriell** (jedes erst nach `/health` des vorigen → kein OOM-Stapel-Peak)
  → bringt den Rest hoch. Erzwingt **genau ein** Hauptmodell und bricht bei zweien ab.
  Voll-Protokoll unter `./logs/`.
- **`./stop.sh`**.
- Modellwechsel: altes `vllm_main*` stoppen (`docker rm -f …`, gibt Port 5568 + VRAM frei),
  `COMPOSE_PROFILES` in `.env` ändern, `./start.sh`.
