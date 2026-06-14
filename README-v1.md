# Eigenständiger, lokaler, DSGVO-orientierter AI-Stack — Anleitung

Ein **komplett selbst-enthaltenes** Bundle, das den Funktionen von claude.ai
nahekommt — **Code-Ausführung & -Korrektur, Artifacts, Skills, fertige
Office-Dateien (.docx/.xlsx/.pptx/.pdf) und Jupyter-Notebooks** — bei
**100 % lokaler Inferenz**, mit **DSGVO-Härtung**, **luftdichter Code-Sandbox**
und **RAGFlow inklusive**. Es setzt nichts aus einem bestehenden Setup voraus:
eigene neue Docker-Netze, eigenes RAGFlow, eigene Volumes.

---

## 1. Architektur

```
   ┌──────────────────── aistack-core (Bridge, MIT Internet) ─────────────────────┐
   │                                                                               │
   │  Open WebUI ──┬─► vllm-main   (Qwen3.5-35B-A3B NVFP4, 26k, FP8-KV)   [GPU]    │
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

   UPGRADE-PROFILE (starten nur auf Wunsch):  fragments · computer-use · morphik
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

## 2. Was du bekommst (claude.ai-Vergleich)

| claude.ai | Hier durch |
|---|---|
| Chat mit starkem Modell | vLLM (Qwen3.5-35B-A3B NVFP4) |
| Artifacts (HTML/SVG/JS) | OWUI Artifacts-Panel · (echte React-Artifacts → Profil `fragments`) |
| Code-Ausführung/-Korrektur | OWUI Code-Interpreter → Jupyter-Sandbox (iteriert bei Fehlern) |
| Skills / Office-Dateien | System-Prompt + Sandbox mit python-docx/openpyxl/python-pptx/reportlab |
| Jupyter-Notebooks | nbformat in der Sandbox |
| Dateien lesen/verarbeiten | OWUI-Upload → Sandbox-Workdir |
| Doku-Wissen (RAG) | **RAGFlow** (eingebettet) · optional Morphik (multimodal) |
| Memory über Sitzungen | Mem0 + Qdrant |
| Agentische RAG/Critic-Pipeline | research-agent (Whitelist-Tools, optional Critic-Loop) |
| Browser/Computer-Use | Profil `computer-use` |

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
```

---

## 4. Projektstruktur

```
local-ai-stack/
├── docker-compose.yml          # Hauptstack + Upgrade-Profile
├── .env.example                # → cp .env.example .env
├── start.sh  stop.sh           # Orchestrierung (Netz → RAGFlow → Stack)
├── README.md
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
├── fragments/                  # Upgrade-Profil A (README + .env.local)
├── open-computer-use/          # Upgrade-Profil B (README)
└── morphik/                    # Upgrade-Profil C (README)
```

---

## 5. Quickstart

```bash
cd local-ai-stack
cp .env.example .env
# Secrets erzeugen und eintragen (WEBUI_SECRET_KEY, SEARXNG_SECRET, JUPYTER_TOKEN):
openssl rand -hex 32

./start.sh            # legt Netz an, startet RAGFlow, dann den Hauptstack
# mit Upgrade(s):  ./start.sh morphik        bzw.  ./start.sh fragments computer-use
```

./start.sh microvm computer-use morphik

docker compose -f docker-compose.yml -f docker-compose.upgrades.yml \
  --profile microvm --profile computer-use --profile morphik up -d vllm-main vllm-helper




`start.sh` erledigt: `docker network create aistack-rag` → RAGFlow hoch
(`./ragflow`) → Hauptstack hoch. Beim ersten Mal laden vLLM/Embeddings die
Modelle von HuggingFace (danach `HF_HUB_OFFLINE=1` in der Compose → offline).

### RAGFlow erstmalig einrichten (einmalig, ~2 Min.)
RAGFlow bringt **keine** Modelle mit — du verkabelst es mit deinem vLLM:

1. RAGFlow-UI öffnen: **http://localhost** → Konto anlegen (lokal).
2. **Model Providers** → einen **OpenAI-kompatiblen** Anbieter hinzufügen:
   - Chat-Modell: Base-URL **`http://host.docker.internal:5568/v1`**, Modell `qwen-main`, Key beliebig.
   - Embedding-Modell: entweder RAGFlows eigenes TEI-Profil aktivieren
     (`COMPOSE_PROFILES=...,tei-gpu` in `ragflow/.env`) **oder** ein
     OpenAI-kompatibles Embedding eintragen (Base-URL `http://host.docker.internal:8082/v1`).
     *(Das ist RAGFlow-intern — getrennt vom `embeddings`-Dienst, der Mem0 versorgt.)*
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
docker compose up -d vllm-main                    # erst das große Modell testen
curl -s http://localhost:5568/v1/models | python -m json.tool
docker compose up -d vllm-helper embeddings qdrant searxng presidio-proxy
docker compose up -d code-sandbox agent open-webui
```

---

## 6. Open WebUI konfigurieren (http://localhost:3009)

1. **Modelle** erscheinen automatisch: `qwen-main`, `qwen-helper`, `research-agent`.
2. **Task-Model** (Titel/Tags): *Admin → Settings → Interface* → **`qwen-helper`**.
3. **Code-Interpreter:** *Admin → Settings → Code Execution* → Engine **Jupyter**,
   URL `http://code-sandbox:8888`, Auth **token**, Token = dein `JUPYTER_TOKEN`.
   *(Die Compose-ENV ist ein Vorschuss; maßgeblich ist die UI — Keys variieren je OWUI-Version. [VERIFY])*
4. **Websuche:** *Admin → Settings → Web Search* → `searxng`,
   Query-URL `http://presidio-proxy:8080/search?q=<query>` → jede Suche wird PII-maskiert.
5. **System-Prompt für Office-Files** (Workspace → Models → `qwen-main`):
   > Wenn der Nutzer Word/Excel/PowerPoint/PDF oder ein Notebook will, SCHREIBE und
   > FÜHRE Python im Code-Interpreter AUS, erzeuge eine ECHTE Datei
   > (python-docx/openpyxl/python-pptx/reportlab/nbformat), speichere sie und nenne
   > den Dateinamen. Bevorzuge polars/duckdb. Bei Fehlern korrigieren und erneut
   > ausführen. Nicht nach Bestätigung fragen — direkt erzeugen.

---

## 7. Die zwei Agent-Varianten (umschaltbar)

In der root-`.env` `AGENT_IMPL` setzen, dann `docker compose up -d agent`:

- **`pydantic`** (Default): schlanker PydanticAI-Agent. Das Modell entscheidet
  selbst, welche der Whitelist-Tools (`retrieve_documents`, `search_web`,
  `run_code`, optional `retrieve_multimodal`) es ruft. Schnell, typsicher.
- **`langgraph`**: echter **Critic-Loop**
  `gather → draft → execute → critic → (ggf. retry)`. Der Agent retrievt,
  entwirft, führt Code in der Sandbox aus, lässt das Ergebnis von einem
  Critic-Schritt streng auf Belegtreue/Vollständigkeit prüfen und überarbeitet
  bis zu 3×. Mehr Latenz, robustere Antworten.

Beide nutzen exakt dieselben Tools und dasselbe Mem0-Gedächtnis (`agent/common.py`).

---

## 8. Upgrade-Profile (gleich im Setup, starten nur auf Wunsch)

| Profil | Befehl | Was | Caveat |
|---|---|---|---|
| **fragments** | `./start.sh fragments` | E2B-Fragments = echte React-Artifacts (Next.js) gegen vLLM | Code-Exec braucht E2B (Firecracker) → voll-lokal aufwendig; sonst bricht Air-Gap. Details: `fragments/README.md` |
| **computer-use** | `./start.sh computer-use` | isolierter Ubuntu-Sandbox: Browser + Office-Skills + Sub-Agenten, in OWUI per MCP | Manager braucht `docker.sock` = Host-Vertrauensgrenze → gVisor/Kata. Details: `open-computer-use/README.md` |
| **morphik** | `./start.sh morphik` | multimodales/visuelles RAG (Bild-/Tabellen-Docs); Agent bekommt Tool `retrieve_multimodal` | `MORPHIK_API_URL` in `.env` setzen + `docker compose up -d agent`. Details: `morphik/README.md` |
| **langgraph-Critic** | `AGENT_IMPL=langgraph` | iterativer Plan→Retrieve→Execute→Critic-Loop | kein Extra-Dienst; nur Env (siehe §7) |

Profile sind in `docker-compose.yml` definiert, laufen aber nur mit `--profile`
(bzw. via `start.sh <profil>`). Der Kernstack bleibt davon unberührt.

---

## 9. DSGVO-/Datensicherheits-Checkliste

- [x] **Inferenz lokal** (vLLM) — keine Cloud-LLM-Calls.
- [x] **Telemetrie aus**: OWUI, Qdrant, Mem0.
- [x] **Websuche PII-maskiert** (Presidio, inkl. AT-VSNR-Recognizer) — OWUI **und** Agent.
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
| vllm-main (35B-A3B NVFP4, 26k, util 0.35) | ~28–31 GB |
| vllm-helper (4B, 32k, util 0.15) | ~10–12 GB |
| embeddings (bge-m3) | ~2–3 GB |
| **Summe / frei** | **~42–46 GB / ~50 GB frei** |

Helfer-Override (dein Template `151000`/`0.40`, ~+30 GB): beim `vllm-helper`
`--max-model-len 151000` und `--gpu-memory-utilization 0.40`.

**RAM/CPU für RAGFlow** (zusätzlich): Elasticsearch ~`MEM_LIMIT` (Default ~8 GB),
plus MySQL/MinIO/Redis; deepdoc-Parsing läuft auf **CPU** (Profil `cpu`). Das
GPU-Profil `gpu` für RAGFlow würde **alle GPUs reservieren** → mit vLLM in
Konflikt; daher bleibt RAGFlow standardmäßig auf CPU.

---

## 12. [VERIFY] — vor Produktivbetrieb prüfen

- **HF-Repo** des Hauptmodells (`nvidia/Qwen3.5-35B-A3B-NVFP4` aktuell?), `--quantization` weglassen.
- **embeddings-Image-Tag** mit SM120-Support; **OpenAI-Route** `/v1/embeddings` von TEI für Mem0
  (sonst Mem0-Embedder auf provider `huggingface` umstellen).
- **RAGFlow-Retrieval-API** (`/api/v1/retrieval`, Payload, API-Key-Format) gegen deine RAGFlow-Version
  — in `agent/common.py` markiert.
- **PydanticAI/Mem0/LangGraph-Versionen** (in `requirements.txt` gepinnt; Kompat-Shim für `.output`/`.data` drin).
- **OWUI Code-Interpreter-ENV-Keys** — maßgeblich ist die Admin-UI.
- **Profil-Images** `fragments`/`open-computer-use`/`morphik` + deren Endpoints — siehe jeweilige README.

---

## 13. Befehle (Spickzettel)

```bash
./start.sh                      # Netz + RAGFlow + Stack
./start.sh morphik              # zusätzlich Morphik-Profil
docker compose ps               # Status Hauptstack
docker compose logs -f vllm-main
docker compose up -d agent      # Agent neu (z.B. nach AGENT_IMPL- oder RAGFLOW_API_KEY-Änderung)
( cd ragflow && docker compose logs -f ragflow-cpu )
./stop.sh                       # alles stoppen (Volumes bleiben)
./stop.sh --volumes             # inkl. Daten löschen
docker compose build --no-cache agent code-sandbox presidio-proxy
```
