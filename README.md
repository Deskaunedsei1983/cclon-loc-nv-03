# Eigenständiger, lokaler, DSGVO-orientierter AI-Stack — Anleitung

Ein **komplett selbst-enthaltenes** Bundle, das den Funktionen von claude.ai
nahekommt — **Code-Ausführung & -Korrektur, Artifacts, Skills, fertige
Office-Dateien (.docx/.xlsx/.pptx/.pdf) und Jupyter-Notebooks** — bei
**100 % lokaler Inferenz**, mit **DSGVO-Härtung**, **luftdichter Code-Sandbox**
und **RAGFlow inklusive**. Es setzt nichts aus einem bestehenden Setup voraus:
eigene neue Docker-Netze, eigenes RAGFlow, eigene Volumes.

> **📚 Detaillierte Dokumentation (aktueller Stand):**
> [`docs/STACK.md`](docs/STACK.md) — Architektur & Funktionen im Detail ·
> [`docs/FLOW.md`](docs/FLOW.md) — Abläufe & Datenflüsse ·
> [`docs/USECASES.md`](docs/USECASES.md) — durchgespielte Use-Cases ·
> [`docs/HOST_UPDATES.md`](docs/HOST_UPDATES.md) — **TODO nach NVIDIA-Treiber-/Kernel-Update** (CUDA-Fix).
> Diese drei spiegeln den jeweils aktuellen Stand (inkl. der drei Hauptmodell-Varianten,
> CPU-Struct-Helfer, Mem0-Selektor, Trust-Liste/Blocklist) und gehen dem README-Text bei
> Abweichungen vor.

---

## 1. Architektur

```
   ┌──────────────────── aistack-core (Bridge, MIT Internet) ─────────────────────┐
   │                                                                               │
   │  Open WebUI ──┬─► vllm-main   (Nemotron NVFP4 | Qwen NVFP4)          [GPU]    │
   │   (:3009)     ├─► vllm-helper (Qwen3.5-4B)                           [GPU]    │
   │               └─► research-agent ─┐                                           │
   │  embeddings(bge-m3)[GPU]  Qdrant(on-disk) ◄─┤ Mem0                            │
   │  presidio-proxy ─► searxng ─► (Web, maskiert) ◄── search-Tool                 │
   │                                   │                                           │
   └───────────────┬───────────────────┼────────────────────────────────────────┘
                   │ (OWUI + Agent)     │ (Agent)
   ┌───────────────┼─ aistack-sandbox (internal, KEIN Internet) ─┐   ┌─ aistack-rag ─┐
   │   code-sandbox: Jupyter(:8888) + /run(:8000), Office-Libs    │   │   research-   │
   │   Code kann NICHTS exfiltrieren                              │   │   agent  ◄────┼─┐
   └─────────────────────────────────────────────────────────────┘   └───────────────┘ │
                                                                                          │
   ┌──────────────── ./ragflow  (eigenes Sub-Bundle, eigenes Netz "ragflow") ───────────┘
   │   ragflow-cpu (:9380 API, :80 UI)  ── es01 · mysql · minio · redis  (intern isoliert)
   └─────────────────────────────────────────────────────────────────────────────────────

   UPGRADE-PROFILE (starten nur auf Wunsch):  microvm · computer-use · morphik
   Agent-Code-Ausfuehrung: Microsandbox-microVM-Executor (Host) statt Subprozess
```

**Schichten:** Inferenz (vLLM) · Gedächtnis (Mem0/Qdrant) · Steuerung
(Agent: PydanticAI **oder** LangGraph-Critic) · Retrieval (RAGFlow, optional
Morphik) · Ausführung (luftdichte Sandbox) · Frontend (Open WebUI).

**Vier eigene Netze:**
- `aistack-core` — Backbone mit Internet (Inferenz, Embeddings, Qdrant, Suche, UI, Agent).
- `aistack-sandbox` — `internal: true`, **kein Egress** → die Code-Sandbox kann
  nichts ins Web schicken (keine PII-Leaks durch generierten Code).
- `aistack-rag` — schmales Brückennetz zwischen Agent und RAGFlow-Server.
- `ragflow` — RAGFlow-intern (ES/MySQL/MinIO/Redis bleiben gekapselt).

---

## 1a. Hauptmodell umschalten (per `.env`)

Das **Haupt-LLM** ist umschaltbar — es laeuft **immer nur eines** (sonst doppelter VRAM).
Die Auswahl steckt in **einer** Zeile der `.env` (Docker-Compose-Profil):

```ini
# genau EINEN Wert setzen:
COMPOSE_PROFILES=main-nemotron,mem0struct     # nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-NVFP4 (Default)
# COMPOSE_PROFILES=main-qwen,mem0struct       # nvidia/Qwen3.6-35B-A3B-NVFP4  + MTP
# COMPOSE_PROFILES=main-qwen-plain,mem0struct # unsloth/Qwen3.6-27B-NVFP4     ohne MTP
```

| Profil | Modell | Kernparameter |
|---|---|---|
| `main-nemotron` **(Default)** | Nemotron-3.5-Lightning-30B-A3B-NVFP4 — hybrides **Mamba-MoE**, 30B total / 3B aktiv | `--kv-cache-dtype fp8` · `--moe-backend marlin` · `--mamba-backend flashinfer` · `--mamba-cache-mode align` · `--enable-prefix-caching` · `--max-num-batched-tokens 16384` · `--reasoning-parser nemotron_v3` · `--tool-call-parser qwen3_coder` · **DSpark**-Speculative-Decoding (3 Tokens) · 260k |
| `main-qwen` | Qwen3.6-35B-A3B-NVFP4 | **MTP**-Speculative-Decoding, `qwen3`-Parser, 260k, KV fp8 |
| `main-qwen-plain` | unsloth/Qwen3.6-27B-NVFP4 | ohne MTP → sauberes Structured Output (gut fuer `mem0 infer=true`) |

- Alle Varianten haengen am **gleichen Hostnamen** `vllm-main:5568` (Netzwerk-Alias)
  und liefern die **stabile Modell-ID `main`** → **Agent, Open WebUI, RAGFlow und
  computer-use bleiben unveraendert**.
- `./start.sh` liest die Auswahl aus der `.env`, erzwingt **genau ein** Hauptmodell
  und **bricht ab**, falls versehentlich mehrere gesetzt sind. Danach laeuft ein
  **automatischer Sanity-Check** (eine Testanfrage + Heuristik), der degenerierte
  Ausgaben („!!!!") sofort meldet.
- Umschalten: Profil in der `.env` aendern, alten Container entfernen
  (`docker rm -f vllm_main vllm_main_qwen_plain vllm_main_nemotron`), dann `./start.sh`.

**Nemotron-Stellschrauben** (`.env`): `NEMOTRON_MAX_LEN` (260000) ·
`NEMOTRON_GPU_UTIL` (0.40) · `NEMOTRON_SPEC_TOKENS` (3).

**vLLM-Version:** Nemotron 3.5 Lightning verlangt laut offizieller vLLM-Recipe
**≥ 0.27.1** — das ist zugleich der aktuelle **stabile** Stand. Ueber `.env` →
**`VLLM_IMAGE`** (`vllm/vllm-openai:v0.27.1`) fuer alle vLLM-Dienste gepinnt.
Neuere Tags: [hub.docker.com/r/vllm/vllm-openai/tags](https://hub.docker.com/r/vllm/vllm-openai/tags).

---

## 2. Was du bekommst (claude.ai-Vergleich)

| claude.ai | Hier durch |
|---|---|
| Chat mit starkem Modell | vLLM (Nemotron-3.5-Lightning-30B-A3B NVFP4 + DSpark) |
| Artifacts (HTML/SVG/JS) | OWUI Artifacts-Panel (HTML/JS/SVG, single-file React via CDN) |
| Code-Ausführung/-Korrektur | OWUI Code-Interpreter → Jupyter-Sandbox (iteriert bei Fehlern); Agent → **Microsandbox-microVM** (hardware-isoliert) |
| Skills / Office-Dateien | System-Prompt + Sandbox mit python-docx/openpyxl/python-pptx/reportlab |
| Jupyter-Notebooks | nbformat in der Sandbox |
| Dateien lesen/verarbeiten | OWUI-Upload → Sandbox-Workdir |
| Doku-Wissen (RAG) | **RAGFlow** (eingebettet) · optional Morphik (multimodal) |
| Memory über Sitzungen | Mem0 + Qdrant |
| Agentische RAG/Critic-Pipeline | research-agent (Whitelist-Tools, optional Critic-Loop) |
| Browser/Computer-Use | `browserless` (laeuft immer, lokal) + Playwright in der Sandbox. Profil `computer-use` ist **nicht lokal** (Anthropic-Cloud-Token noetig) -> nicht in `--all` |

---

## 3. Voraussetzungen (Host: CachyOS + Blackwell)

```bash
# Treiber (offene Module) + Container-Toolkit
sudo pacman -S linux-cachyos-headers nvidia-open-dkms nvidia-utils \
               docker docker-compose docker-buildx nvidia-container-toolkit
echo 'options nvidia_drm modeset=1' | sudo tee /etc/modprobe.d/nvidia.conf
sudo mkinitcpio -P
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"           # danach neu einloggen
sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker
# Test:
docker run --rm --gpus all nvidia/cuda:13.0.0-base-ubuntu24.04 nvidia-smi

# PFLICHT für RAGFlows Elasticsearch:
sudo sysctl -w vm.max_map_count=262144
echo 'vm.max_map_count=262144' | sudo tee /etc/sysctl.d/99-ragflow.conf   # persistent

# Für den microVM-Executor (Microsandbox, KVM/libkrun) — Host-Betrieb empfohlen:
ls -l /dev/kvm                                   # vorhanden? (Blackwell/CachyOS: ja)
groups | grep -q kvm || sudo usermod -aG kvm "$USER"     # danach neu einloggen
curl -sSL https://get.microsandbox.dev | sh      # Microsandbox-Runtime (libkrun)
# Executor-Dienst starten: siehe microsandbox-executor/README.md
```

---

## 4. Projektstruktur

```
local-ai-stack/
├── docker-compose.yml          # KERNSTACK (sauber, ohne Upgrade-Pfade)
├── docker-compose.upgrades.yml # alle Upgrade-Pfade (Overlay, nur auf Wunsch)
├── docker-compose.observability.yml # zentrales Logging (Loki+Grafana+Dozzle+Promtail)
├── .env.example                # → cp .env.example .env
├── start.sh  stop.sh           # Orchestrierung (Netz → RAGFlow → Stack [+Overlay])
├── README.md
├── observability/              # Logging-Stack: Configs + fertiges Grafana-Dashboard
│   ├── loki/  promtail/  grafana/   README.md
├── ragflow/                    # EIGENES RAGFlow (Original-Upstream eingebettet)
│   ├── docker-compose.yml  docker-compose-base.yml  docker-compose.override.yml
│   ├── .env  service_conf.yaml.template  entrypoint.sh  init.sql
├── agent/                      # Controller (umschaltbar)
│   ├── common.py               #   Config + Mem0 + Tool-Funktionen (geteilt)
│   ├── agent_pydantic.py       #   Variante 1: PydanticAI
│   ├── agent_langgraph.py      #   Variante 2: LangGraph-Critic-Loop
│   ├── server.py               #   OpenAI-kompatibel, wählt Variante per AGENT_IMPL
│   ├── requirements.txt  Dockerfile
├── code-sandbox/               # luftdichte Jupyter+/run-Sandbox (Office-Libs)
├── presidio-proxy/             # PII-Maskierung vor der Websuche
├── searxng/                    # settings.yml (JSON-API an)
├── microsandbox-executor/      # microVM-Executor (Microsandbox) für den Agent
│   ├── run_api.py  requirements.txt  Dockerfile  README.md
├── open-computer-use/          # Upgrade-Profil (README)
└── morphik/                    # Upgrade-Profil (README)
```

> Tika (Dokument-Extraktion + OCR für **direkte OWUI-Uploads**) ist ein Kern-
> Dienst in `docker-compose.yml` und an OWUI verdrahtet. Es ist **nicht** für
> RAGFlow/Morphik nötig (die parsen selbst) und nichts für Mem0 — nur für Dateien,
> die du direkt in OWUI hochlädst. Bei Bedarf den `tika`-Dienst einfach weglassen.

---

## 5. Quickstart

```bash
cd local-ai-stack
cp .env.example .env
# Secrets erzeugen und eintragen (WEBUI_SECRET_KEY, SEARXNG_SECRET, JUPYTER_TOKEN):
openssl rand -hex 32

./start.sh            # Netz + RAGFlow + Kernstack + Observability + .env-Profile
./start.sh --all      # ALLES: zusaetzlich jedes optionale Profil (siehe Tabelle)
./start.sh morphik microvm   # gezielt einzelne Profile ergaenzen

./stop.sh             # stoppt ALLES (alle Profile + RAGFlow); Daten bleiben
./stop.sh --volumes   # zusaetzlich Volumes loeschen (DATENVERLUST!)
```

**Was startet wann?**

| | ohne Argument | `--all` |
|---|---|---|
| Kern (Agent, OWUI, Code-Sandbox, SearXNG, Presidio, Qdrant, **vllm-embed**, **embeddings/TEI**, ingest-router, browserless) | ✅ immer | ✅ |
| Observability (Grafana/Loki/Promtail/Prometheus/Exporter/Dozzle/netdata) | ✅ immer (`LOGGING_STACK=0` schaltet ab) | ✅ |
| RAGFlow (eigenes Sub-Bundle: ragflow-cpu + ES/MySQL/MinIO/Redis) | ✅ immer | ✅ |
| Hauptmodell | das **eine** aus `COMPOSE_PROFILES` | dito — die anderen **nie** (exklusiv) |
| `mem0struct` (CPU-JSON-Helfer) | wenn in `COMPOSE_PROFILES` | ✅ |
| `morphik`, `microvm`, `fragments` | nur per CLI-Argument | ✅ |
| `blocklist` | wenn in `COMPOSE_PROFILES` | ✅ **nur wenn `BLOCKLIST_URL` gesetzt** |
| `helper` (GPU-Modell Qwen3.5-4B) | wenn in `COMPOSE_PROFILES` | ❌ **bewusst nicht** — redundant zu `mem0-struct`, kostet ~14 GB VRAM. Nachrüstbar: `./start.sh --all helper` |
| `computer-use` | nur per CLI-Argument | ❌ **bewusst nicht** — verlangt `ANTHROPIC_AUTH_TOKEN` (Cloud-API) + `GITLAB_TOKEN`; widerspricht der Vorgabe 100 % lokal |

**Welche Modelle lädt der Stack?** `start.sh` listet das beim Start auf. Im
Default (`main-nemotron,mem0struct`) sind es vier — drei auf der GPU, eines auf der CPU:

| Modell | Dienst | Wofür | Last |
|---|---|---|---|
| Nemotron-3.5-Lightning-30B-A3B-NVFP4 **+ DSpark-Draft** | `vllm-main-nemotron` | Chat/Agent (`main`) | GPU, `NEMOTRON_GPU_UTIL` (0.40 ≈ 38 GB) |
| `EMBED_MODEL` (Qwen3-Embedding-4B, 2560 Dim) | `vllm-embed` | **RAGFlow + Morphik** | GPU, `EMBED_GPU_UTIL` (0.15 ≈ 14 GB) |
| BAAI/bge-m3 (1024 Dim) | `embeddings` (TEI) | **nur Mem0** | GPU, ~2–3 GB |
| `MEM0_STRUCT_HF` (Qwen2.5-3B GGUF) | `mem0-struct` | mem0-JSON + OWUI-Tasks | **CPU**, ~0 VRAM |

Mit `--all` kommt ColPali über Morphik dazu — **rechne ~14 GB GPU**, nicht 7–8: Morphik
lädt das Modell **zweimal** (ARQ-Worker *und* Uvicorn-Server). Reicht der freie VRAM
nicht, stirbt Morphik mit `torch.OutOfMemoryError` und startet in einer Schleife neu.
Erster Hebel: `helper` weglassen (spart ~14 GB und ist ohnehin redundant). OWUI nutzt für Chat-Uploads zusätzlich sein eingebautes
`all-MiniLM-L6-v2` (CPU) — die drei Embedder haben **verschiedene Dimensionen** und
sind bewusst getrennt.

`start.sh` erledigt: `docker network create aistack-rag` → RAGFlow hoch
(`./ragflow`) → Hauptstack hoch. Die Profile aus der `.env` werden **explizit** als
`--profile` durchgereicht (sonst hängt es an der Compose-Version, ob
`COMPOSE_PROFILES` und `--profile` vereinigt werden). Beim ersten Mal laden vLLM/Embeddings die
Modelle von HuggingFace (danach `HF_HUB_OFFLINE=1` in der Compose → offline).

### RAGFlow erstmalig einrichten (einmalig, ~2 Min.)
RAGFlow bringt **keine** Modelle mit — du verkabelst es mit deinem vLLM:

1. RAGFlow-UI öffnen: **http://localhost** → Konto anlegen (lokal).
2. **Model Providers** → einen **OpenAI-kompatiblen** Anbieter hinzufügen:
   - Chat-Modell: Base-URL **`http://host.docker.internal:5568/v1`**, Modell `main`, Key beliebig.
     *(`main` zeigt immer auf das aktive Hauptmodell — überlebt das Profil-Umschalten.)*
   - Embedding-Modell (**empfohlen**): den starken, multilingualen vLLM-Embedder
     eintragen — OpenAI-kompatibel, Base-URL **`http://host.docker.internal:8091/v1`**,
     Modell **`qwen3-embed`**, Dimension **4096**. *(Das ist der `vllm-embed`-Dienst,
     Qwen3-Embedding-8B, MTEB-#1-Text; deutlich stärker als bge-m3.)*
     **Wichtig:** Das Embedding-Modell ist **pro Knowledge-Base fix** — beim Wechsel
     (andere Dimension!) eine **neue KB** anlegen und Dokumente neu parsen.
     Alternativen: RAGFlows eigenes TEI-Profil (`COMPOSE_PROFILES=...,tei-gpu` in
     `ragflow/.env`) oder das bge-m3-`embeddings` (`…:8082/v1`, versorgt sonst Mem0).
3. **Dataset** anlegen, Dokumente hochladen, Parsing abwarten.
4. **API-Key** erzeugen (RAGFlow → Settings → API) und die **Dataset-ID** notieren.
5. In die root-`.env`: `RAGFLOW_API_KEY=...` und `RAGFLOW_DATASET_IDS=...`, dann:
   ```bash
   docker compose up -d agent          # Agent mit RAGFlow-Anbindung neu starten
   ```

> RAGFlow erreicht dein vLLM über `host.docker.internal` (Host-Port); der **Agent**
> erreicht RAGFlow über das gemeinsame Netz unter `http://ragflow-cpu:9380`.

### Alternativ: manuell/inkrementell hochfahren
```bash
docker network create aistack-rag
( cd ragflow && docker compose up -d )            # RAGFlow
docker compose --profile main-nemotron up -d vllm-main-nemotron   # Hauptmodell (bzw. --profile main-qwen up -d vllm-main)
curl -s http://localhost:5568/v1/models | python -m json.tool
docker compose up -d vllm-helper embeddings qdrant searxng presidio-proxy
docker compose up -d code-sandbox agent open-webui
```

---

## 5b. Zentrales Logging / Debugging — „wo muss ich schauen?"

Damit nie wieder unklar ist, **in welchem Container** das Problem steckt (z. B.
Websuche liefert nichts), laufen die Logs **aller** Container an einer Stelle
zusammen. Der Stack startet **automatisch mit** (`./start.sh`), Details:
[`observability/README.md`](observability/README.md).

| Dienst | Zweck | Zugriff |
|--------|-------|---------|
| **Grafana** | **Logs** (Loki) **+ Metriken** (Prometheus), persistent, fertige Dashboards | http://localhost:3011 |
| **Dozzle**  | **Live**-Viewer aller Container-Logs in Echtzeit | http://localhost:8085 |
| **Netdata** | **Live**-Metriken mit eigener UI: GPU/VRAM, Disk-I/O, Netz in/out (auto) | http://localhost:19999 |
| Loki/Promtail · Prometheus + node/cadvisor/nvidia-Exporter | Speicher + Sammler für Logs bzw. Metriken (Host, je Container, GPU) | intern |

> Was Dozzle **nicht** kann (GPU/VRAM, Disk-Raten, Netz in/out), liefern **Netdata**
> (sofort, eigene UI) und das Grafana-Dashboard **„Host & GPU"** (Prometheus). Details:
> [`observability/README.md`](observability/README.md).

**Triage in 10 Sekunden:** Grafana → Dashboard **„AI-Stack — Container-Logs &
Fehler"**:
- Panel **„Fehler / Warnungen pro Container"** → zeigt sofort den schuldigen Dienst.
- Panel **„Websuche-Pfad"** → bündelt `open-webui` · `presidio_proxy` · `searxng`.
  Der Proxy loggt jetzt die **SearXNG-Treffer-Anzahl** — `n=0` ⇒ Engines
  leer/rate-limitiert ⇒ genau das ist OWUIs „404: No results found from web search".

**Log-Level:** Such-/RAG-/Agent-Pfad steht per Default auf **DEBUG**
(`OWUI_LOG_LEVEL`, `PRESIDIO_LOG_LEVEL`, `SEARXNG_DEBUG`, `AGENT_LOG_LEVEL` in der
`.env`). **vLLM-Dienste bleiben bewusst unangetastet** (loggen ohnehin viel).
Logging ganz aus: `LOGGING_STACK=0 ./start.sh`.

---

## 6. Open WebUI konfigurieren (http://localhost:3009)

1. **Modelle** erscheinen automatisch: `main` (= aktives Hauptmodell),
   `qwen-helper`, `research-agent`.
2. **Task-Model** (Titel/Tags): *Admin → Settings → Interface* → **`qwen-helper`**.
3. **Code-Interpreter:** *Admin → Settings → Code Execution* → Engine **Jupyter**,
   URL `http://code-sandbox:8888`, Auth **token**, Token = dein `JUPYTER_TOKEN`.
   *(Die Compose-ENV ist ein Vorschuss; maßgeblich ist die UI — Keys variieren je OWUI-Version. [VERIFY])*
4. **Websuche** läuft **automatisch aus der Compose-ENV** — **kein UI-Toggle nötig**,
   weil `ENABLE_PERSISTENT_CONFIG=False` gesetzt ist (OWUI liest die Settings aus der
   ENV statt aus seiner DB). Die Kette:
   - SearXNG hinter dem **PII-Maskier-Proxy** (`http://presidio-proxy:8080/search?q=<query>`),
   - **volle, JS-gerenderte Seiteninhalte** über den **`browserless`**-Headless-Chromium
     (`WEB_LOADER_ENGINE=playwright`, `PLAYWRIGHT_WS_URI=ws://browserless:3000/chromium/playwright?token=…`)
     → echte Tabellen/Zahlen statt leerer HTML-Hüllen,
   - RAG-Relevanzfilter umgangen (`BYPASS_WEB_SEARCH_EMBEDDING_AND_RETRIEVAL=true`),
     damit die Treffer auch im Modell-Kontext landen.

   Tuning: `WEB_SEARCH_RESULT_COUNT`, `WEB_LOADER_CONCURRENT_REQUESTS`, browserless `TIMEOUT`.
   **Fallback** bei Loader-Fehlern („An error occurred"/„No results"): `BYPASS_WEB_SEARCH_WEB_LOADER=true`
   (nur Snippets, robust, aber flacher). *(Das mcr-Playwright-`run-server` zerbrach an OWUI 0.9.5
   per Versions-Mismatch — browserless ist CDP-/versions-tolerant.)*

   **„0 Treffer trotz funktionierender Pipeline"?** Dann blockt die **Server-IP**
   die freien Engines „soft" (SearXNG = HTTP 200 + 0 Treffer, kein 429). Statt am
   Engine-Satz zu drehen, auf eine **offizielle Such-API** umschalten — der
   Presidio-Proxy spricht sie direkt an und **maskiert weiterhin zuerst** (kein
   PII-Leak), OWUI bekommt dasselbe JSON. In der `.env`:
   ```bash
   SEARCH_BACKEND=brave      # oder: tavily | serper | google_pse
   BRAVE_API_KEY=dein-key    # je nach Backend der passende Key (siehe .env-example)
   docker compose up -d --force-recreate presidio-proxy
   ```
   So bleibt SearXNG der kostenlose Default, die API ist der IP-unabhaengige
   Notnagel — umschaltbar per ENV, ohne Code-Aenderung.
5. **System-Prompt für Office-Files** (Workspace → Models → `main`):
   > Wenn der Nutzer Word/Excel/PowerPoint/PDF oder ein Notebook will, SCHREIBE und
   > FÜHRE Python im Code-Interpreter AUS, erzeuge eine ECHTE Datei
   > (python-docx/openpyxl/python-pptx/reportlab/nbformat), speichere sie und nenne
   > den Dateinamen. Bevorzuge polars/duckdb. Bei Fehlern korrigieren und erneut
   > ausführen. Nicht nach Bestätigung fragen — direkt erzeugen.

---

## 7. Die zwei Agent-Varianten (umschaltbar)

In der root-`.env` `AGENT_IMPL` setzen, dann `docker compose up -d agent`:

- **`langgraph`** (Default): echter **Critic-Loop**
  `gather → draft → execute → critic → (ggf. retry)`. Der Agent retrievt,
  entwirft, führt Code in der Sandbox aus, lässt das Ergebnis von einem
  Critic-Schritt streng auf Belegtreue/Vollständigkeit prüfen und überarbeitet
  bis zu 3×. Robustere Antworten, dafür mehr LLM-Aufrufe/Latenz pro Anfrage.
- **`pydantic`**: schlanker PydanticAI-Agent. Das Modell entscheidet selbst,
  welche der Whitelist-Tools (`retrieve_documents`, `search_web`, `run_code`,
  optional `retrieve_multimodal`) es ruft. Schneller, typsicher.

Beide nutzen exakt dieselben Tools und dasselbe Mem0-Gedächtnis (`agent/common.py`).

---

## 8. Upgrade-Profile (gleich im Setup, starten nur auf Wunsch)

| Profil | Befehl | Was | Caveat |
|---|---|---|---|
| **microvm** | `./start.sh microvm` | Microsandbox-microVM-Executor als Container (statt Host-Betrieb) | braucht `/dev/kvm` + NET_ADMIN; Host-Betrieb ist stabiler. Details: `microsandbox-executor/README.md` |
| **computer-use** | `./start.sh computer-use` | isolierter Ubuntu-Sandbox: Browser + Office-Skills + Sub-Agenten, in OWUI per MCP | Manager braucht `docker.sock` = Host-Vertrauensgrenze → gVisor/Kata. Details: `open-computer-use/README.md` |
| **morphik** | `./start.sh morphik` | multimodales/visuelles RAG (Bild-/Tabellen-Docs); Agent bekommt Tool `retrieve_multimodal` | `MORPHIK_API_URL` in `.env` setzen + `docker compose up -d agent`. Details: `morphik/README.md` |
| **langgraph-Critic** | `AGENT_IMPL=langgraph` (Default) | iterativer Plan→Retrieve→Execute→Critic-Loop | kein Extra-Dienst; nur Env (siehe §7) |

> Der microVM-Executor läuft **bevorzugt auf dem Host** (siehe
> `microsandbox-executor/README.md`); das Profil `microvm` ist nur die optionale
> Container-Variante. Der Agent nutzt ihn automatisch über `MSB_EXECUTOR_URL`.

Die Upgrade-Pfade liegen jetzt in einer **eigenen Overlay-Datei**
(`docker-compose.upgrades.yml`) — der Kernstack (`docker-compose.yml`) bleibt
übersichtlich und enthält keinen Upgrade-Ballast. `./start.sh <profil>` bindet
das Overlay automatisch ein; manuell:

```bash
docker compose -f docker-compose.yml -f docker-compose.upgrades.yml --profile morphik up -d
```

Ohne Profil startet **nichts** aus dem Overlay; ein blankes `docker compose up -d`
lädt das Overlay gar nicht erst. Der Kernstack bleibt unberührt.

### Was verlieren wir durch den Verzicht auf Fragments — und die Alternativen

Fragments lieferte die **„generative-UI-Artifacts"**: das Modell schreibt eine
**komplette, lauffähige Mini-Web-App** (Next.js/React, Streamlit, Gradio, Vue …),
die **live gerendert, interaktiv und im Browser editier-/neu-ausführbar** ist —
mit Code-Ausführung in einer Sandbox. Konkret verlieren wir nur:

1. **Voll lauffähige, mehrdateiige Web-Apps** aus einem Prompt (Next.js/React mit
   Build- und Server-Runtime) inkl. Live-Preview.
2. Die **Fragment-Templates** (Streamlit/Gradio/Vue …) mit Ein-Klick-Vorschau.
3. **In-Browser-Editieren + erneutes Ausführen** der erzeugten App.

**Was wir NICHT verlieren** (und wodurch es abgedeckt ist):

| Funktion | Ersatz im Stack |
|---|---|
| Interaktive **HTML/CSS/JS**-Artifacts, Diagramme, kleine Tools | **OWUI Artifacts-Panel** (rendert HTML/JS/SVG inline) |
| **Single-file React** (Komponente/Widget) | OWUI-Artifact mit React via CDN (genügt für die meisten Fälle) |
| **Code ausführen/korrigieren** | OWUI Code-Interpreter → Jupyter-Sandbox; Agent → microVM |
| **Office-Dateien** (.docx/.xlsx/.pptx/.pdf) | OWUI Code-Interpreter (python-docx/openpyxl/python-pptx/reportlab) |
| **Jupyter-Notebooks**, Daten-Viz | nbformat + matplotlib in der Sandbox |

Es fehlt also praktisch nur das **vollwertige, mehrdateiige Live-Web-App-Artifact**.
Drei Wege, das bei Bedarf nachzurüsten:
- **Pragmatisch (empfohlen):** im System-Prompt das Modell anweisen, interaktive
  Tools als **eigenständige HTML-Datei (React per CDN, Tailwind per CDN)** zu
  erzeugen → rendert direkt im OWUI-Artifacts-Panel. Deckt ~90 % der „Artifact"-Fälle ab.
- **Mittel:** eine kleine statische Vorschau bauen — das Modell erzeugt die App per
  `run_code` in der Sandbox und legt sie als servierbares Bundle ab.
- **Voll:** Fragments doch betreiben — dann aber nur mit **selbst gehostetem E2B**
  (Firecracker, aufwendig, Cloudflare-Domain nötig) oder einem **Fork von Fragments**,
  der dessen E2B-Aufrufe auf Microsandbox umbiegt (echte Entwicklungsarbeit).

---

## 9. DSGVO-/Datensicherheits-Checkliste

- [x] **Inferenz lokal** (vLLM) — keine Cloud-LLM-Calls.
- [x] **Telemetrie aus**: OWUI, Qdrant, Mem0.
- [x] **Websuche PII-maskiert** (Presidio, inkl. AT-VSNR-Recognizer) — OWUI **und** Agent.
- [x] **Websuche-Engines kuratiert** (SearXNG: `mojeek`/`brave`/`bing`): zuverlässig aus
      Server-IP, ohne Dauer-CAPTCHA/403-Rauschen. *(Tor wurde getestet und wieder
      entfernt — Exit-IPs werden von den Engines soft-geblockt → leere Treffer.)*
- [x] **Code-Sandbox luftdicht** (`aistack-sandbox`, internal) — kein Egress.
- [x] **RAGFlow-Backend isoliert** im eigenen `ragflow`-Netz; nur der Server hängt am Brückennetz.
- [x] **DBs auf NVMe** statt RAM (siehe §10).
- [ ] **RAGFlow-Default-Passwörter ändern** (`ragflow/.env`: `ELASTIC_PASSWORD`,
      `MYSQL_PASSWORD`, `MINIO_PASSWORD`, `REDIS_PASSWORD`) — Default ist `infini_rag_flow`!
- [ ] **Härten**: Ports an `127.0.0.1` binden, gVisor für die Sandbox
      (`runtime: runsc`), Volumes verschlüsseln. DSGVO-Löschrecht: `memory.delete(user_id=...)`.
- [ ] **Offline** nach Modell-Download: `HF_HUB_OFFLINE=1` in der Compose.
- [ ] Presidio ist sehr gut, aber kein 100 %-Garant — bei Maximalanspruch Websuche deaktivieren.

---

## 10. NVMe statt RAM bei den DBs

- **Qdrant** mit `on_disk: true` (Vektoren/Payload memmap auf SSD) statt reinem RAM.
- **Mem0** ist **vektor-only** (kein Neo4j/**Memgraph** — Memgraph ist in-memory → RAM-hungrig).
- **OWUI** nutzt SQLite (Datei im Volume).
- Bewusst **keine** reinen In-RAM-Vektor-DBs.
- Hinweis: **RAGFlow** ist davon unabhängig RAM-/CPU-intensiv (siehe §11).

---

## 11. Ressourcen-Budget

**VRAM (96 GB)** mit Defaults:

| Komponente | VRAM ca. |
|---|---|
| vllm-main (Qwen 35B-A3B NVFP4, 26k, util 0.35, **Vision an** + MTP-Draft) | ~30–34 GB |
| *— ODER —* vllm-main-nemotron (Nemotron 30B-A3B NVFP4 + DSpark, **260k**, util 0.40, max-num-seqs 2) | ~34–38 GB |
| vllm-helper (4B, 32k, util 0.15) | ~10–12 GB |
| embeddings (bge-m3, Mem0) | ~2–3 GB |
| vllm-embed (Qwen3-Embedding-8B, FP8, util-Deckel 0.25, RAGFlow) | ~10–13 GB real |
| **Summe / frei** | **~55–61 GB / ~35 GB frei** |

> `--gpu-memory-utilization` ist nur eine **Obergrenze**, kein fixer Verbrauch — Embedding-
> Modelle füllen sie (kein KV-Cache) nicht aus. Realen Wert mit `nvidia-smi` prüfen.

> **Die Hauptmodelle schließen sich aus** (Profil-Umschaltung) →
> es liegt **nie mehr als ein** Hauptmodell im VRAM. `util 0.35` deckelt die
> Reservierung; der Startlog zeigt die tatsächliche KV-Cache-Größe.

Hinweise zum Hauptmodell: der **Vision-Encoder** ist geladen (Bildverstehen) und
kostet etwas extra; die **MTP-Spekulation** (`--speculative-config`) hält ein
kleines Draft-Modul vor (mehr Speed, minimal mehr VRAM). Beides ist dein
bewährtes Setup.

Helfer-Override (dein Template `151000`/`0.40`, ~+30 GB): beim `vllm-helper`
`--max-model-len 151000` und `--gpu-memory-utilization 0.40`.

**RAM/CPU für RAGFlow** (zusätzlich): Elasticsearch ~`MEM_LIMIT` (Default ~8 GB),
plus MySQL/MinIO/Redis; deepdoc-Parsing läuft auf **CPU** (Profil `cpu`). Das
GPU-Profil `gpu` für RAGFlow würde **alle GPUs reservieren** → mit vLLM in
Konflikt; daher bleibt RAGFlow standardmäßig auf CPU.

**RAGFlow im RAM-Sparmodus (≤ 20 GB, „HDD vor RAM"):**
- **`DOC_ENGINE=infinity`** in `ragflow/.env` statt `elasticsearch` → disk-orientierte
  Vektor-DB, deutlich weniger RAM als ES' JVM-Heap. **Der größte Hebel.** (Backend-Wechsel
  = neu ingestieren — passt, du baust die KB ohnehin neu.)
- Bleibst du bei ES: `MEM_LIMIT` deckeln (z. B. `6442450944` = 6 GB).
- **Knowledge Graph (GraphRAG) und RAPTOR NICHT generieren** — die größten RAM-/Storage-/
  Zeit-Fresser (LLM pro Chunk + Graph/Baum-Speicher). Nur bei echtem Bedarf einschalten.
- Dataset-Tuning: **Auto-question = 0**, **Auto-keyword = 0**, **Auto-metadata aus**;
  **Child-Chunks aus** halbiert den Index (kostet etwas Retrieval-Präzision); **Overlap 0**.
- **Language = Deutsch** (nicht English) bei deutschen Dokumenten → bessere Tokenisierung/Qualität.
- deepdoc bleibt **CPU** (`DEVICE=cpu`) → kein VRAM-Konflikt mit vLLM.
- 4096-dim-Vektoren (8B-Embed) sind der schwerste Index-Teil; mit Infinity (Disk) ok,
  sonst via MRL auf 2048 reduzieren.

---

## 12. [VERIFY] — vor Produktivbetrieb prüfen

- **HF-Repo** des Hauptmodells (Repo-Namen bei Modellwechsel prüfen).
- **`--quantization modelopt`**: falls vLLM meckert, `modelopt_fp4` probieren.
- **`--speculative-config` (MTP)**: nur wenn die NVFP4-Checkpoints die
  MTP-Module enthalten — sonst beim Laden Fehler → Flag entfernen.
- **Nemotron 3.5 Lightning** (`COMPOSE_PROFILES=main-nemotron`): Flags stammen aus der
  offiziellen vLLM-Recipe (`vllm-project/recipes`, Variante NVFP4) — bei einem vLLM- oder
  Modell-Update dort gegenprüfen. Der `--tool-call-parser` ist **`qwen3_coder`** laut
  NVIDIA-Modellkarte (die Recipe nennt abweichend `qwen3_xml`). Als **verifizierte**
  Hardware nennt die Recipe H100 / DGX Spark / DGX Station — RTX PRO 6000 (SM120) ist
  dort **nicht** gelistet; `--moe-backend marlin` ist auf SM120 aber genau der
  funktionierende Pfad. Bei degeneriertem Output (der Sanity-Check meldet es):
  `VLLM_USE_DEEP_GEMM=0` im Service aktivieren.
- **Vision**: KEIN `--language-model-only` (Vision-Encoder bleibt geladen).
  Falls du Bilder bewusst sperren willst: `--limit-mm-per-prompt '{"image":0}'`.
- **`--safetensors-load-strategy prefetch`**: existiert ab neueren vLLM-Builds
  (du hattest es im Einsatz) — bei älterem vLLM ggf. entfernen.
- **embeddings-Image-Tag** mit SM120-Support; **OpenAI-Route** `/v1/embeddings` von TEI für Mem0
  (sonst Mem0-Embedder auf provider `huggingface` umstellen).
- **vllm-embed** (`vllm-embed`, Qwen3-Embedding-4B): Flag `--runner pooling` (aktuelles vLLM)
  vs. `--task embed` (älter) gegen dein Nightly prüfen; `/v1/embeddings` testen:
  `curl -s localhost:8091/v1/embeddings -H 'Content-Type: application/json' -d '{"model":"qwen3-embed","input":"hallo welt"}' | head -c 200`.
  `--quantization fp8` notfalls weglassen (dann FP16, ~16 GB). In RAGFlow die
  **Dimension 4096** (8B) eintragen; bei Wechsel auf 4B wäre es 2560.
- **RAGFlow-Retrieval-API** (`/api/v1/retrieval`, Payload, API-Key-Format) gegen deine RAGFlow-Version
  — in `agent/common.py` markiert.
- **PydanticAI/Mem0/LangGraph-Versionen** (in `requirements.txt` gepinnt; Kompat-Shim für `.output`/`.data` drin).
- **OWUI Code-Interpreter-ENV-Keys** + **Tika-Engine-Key** (`CONTENT_EXTRACTION_ENGINE`) — maßgeblich ist die Admin-UI.
- **Microsandbox** (`microsandbox==0.5.5`): SDK-Verbindungs-/Server-Modell und Output-Attribute
  (`stdout_text`/`exit_code`) gegen die installierte Version prüfen; `/dev/kvm` + kvm-Gruppe
  vorhanden; für Office-Files ein OCI-Image mit den Libs setzen (`MSB_IMAGE`).
- **Profil-Images** `open-computer-use`/`morphik` + deren Endpoints — siehe jeweilige README.

---

## 13. Befehle (Spickzettel)

```bash
./start.sh                      # Netz + RAGFlow + sauberer Kernstack
./start.sh morphik              # Kern + Upgrade-Overlay (Profil morphik)
./start.sh microvm computer-use morphik   # Kern + ALLE Upgrade-Pfade (ein Befehl)
# manuell mit Overlay:
docker compose -f docker-compose.yml -f docker-compose.upgrades.yml --profile morphik up -d
docker compose ps               # Status Kernstack
docker compose logs -f vllm-main          # Qwen-Hauptmodell (COMPOSE_PROFILES=main-qwen)
docker compose logs -f vllm-main-nemotron # Nemotron-Hauptmodell (COMPOSE_PROFILES=main-nemotron)
# Hauptmodell umschalten: in .env COMPOSE_PROFILES=main-nemotron|main-qwen|main-qwen-plain -> ./start.sh
docker compose up -d agent      # Agent neu (z.B. nach AGENT_IMPL- oder RAGFLOW_API_KEY-Änderung)
( cd ragflow && docker compose logs -f ragflow-cpu )
./stop.sh                       # alles stoppen (Volumes bleiben)
./stop.sh --volumes             # inkl. Daten löschen
docker compose build --no-cache agent code-sandbox presidio-proxy
```

> **`git pull` scheitert an `searxng/settings.yml` („Keine Berechtigung")?** Der
> SearXNG-Container läuft als UID 977 und patcht `settings.yml` beim Start per
> `sed -i` → er würde die git-Datei chownen. Deshalb mountet der Container jetzt
> `searxng/runtime/` (gitignored), **nicht** die git-Datei; `./start.sh` spiegelt
> `searxng/settings.yml` dorthin. So bleibt `git pull` **ohne `sudo`**. Nur die
> SearXNG-Settings geändert und schnell neu laden (ohne ganzes `start.sh`):
> ```bash
> ./reload-searxng.sh        # spiegelt -> recreate -> verifiziert Engine-Satz + Live-Test
> ```
> (manuell-äquivalent: `cp -f searxng/settings.yml searxng/runtime/settings.yml`
> dann `docker compose -f docker-compose.yml up -d --force-recreate --no-deps searxng`).
> **Wichtig:** Ein blosses `docker compose restart searxng` ohne vorheriges
> Spiegeln nutzt die ALTE `runtime/`-Kopie → Änderungen scheinen "nicht zu wirken".
> Einmalig die alten, vom Container gechownten Dateien zurückholen (falls noch nötig):
> `sudo chown -R "$USER:$USER" searxng/`
