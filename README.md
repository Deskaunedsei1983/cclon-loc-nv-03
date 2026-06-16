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
   │  Open WebUI ──┬─► vllm-main   (Qwen NVFP4 | Gemma-Diffusion NVFP4)   [GPU]    │
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

## 1a. Hauptmodell umschalten: Qwen ⇄ Gemma-Diffusion (per `.env`)

Das **Haupt-LLM** ist umschaltbar — entweder das bisherige **Qwen** *oder* das
**Gemma-Diffusion**-Modell, **nie beide gleichzeitig** (sonst doppelter VRAM).
Die Auswahl steckt in **einer** Zeile der `.env` (Docker-Compose-Profil):

```ini
# genau EINEN Wert setzen:
COMPOSE_PROFILES=main-qwen     # nvidia/Qwen3.6-35B-A3B-NVFP4           (Standard)
# COMPOSE_PROFILES=main-gemma  # nvidia/diffusiongemma-26B-A4B-IT-NVFP4 (Diffusion, 256k)
```

- Beide Varianten hängen am **gleichen Hostnamen** `vllm-main:5568` (Netzwerk-Alias)
  und liefern die **stabile Modell-ID `main`** → **Agent, Open WebUI, RAGFlow und
  computer-use bleiben unverändert**. (Qwen antwortet zusätzlich weiter auf `qwen-main`,
  Gemma zusätzlich auf `gemma-main`.)
- `./start.sh` liest die Auswahl aus der `.env`, erzwingt **genau ein** Hauptmodell
  (Default `main-qwen`) und **bricht ab**, falls versehentlich beide gesetzt sind.
- Helfer-LLM, Embeddings, Agent usw. laufen davon **unabhängig** weiter.
- Umschalten (altes stoppen, neues starten):
  ```bash
  # in .env COMPOSE_PROFILES=main-gemma setzen, dann z.B.:
  docker compose -f docker-compose.yml --profile main-qwen  down            # altes Hauptmodell weg
  docker compose -f docker-compose.yml --profile main-gemma up -d vllm-main-gemma
  # bequemer (macht beides passend):  ./start.sh
  ```

**Gemma-Start — an SM120/Blackwell, 256k & VRAM-Sparsamkeit angepasst (Kernparameter):**
`--max-model-len 262144` (256k) · `--gpu-memory-utilization 0.35` ·
`--max-num-seqs 2` (keine Multi-Request-/VRAM-Überbelegung, „wie bisher"; Model-Card
nennt 4) · Env `VLLM_USE_V2_MODEL_RUNNER=1` · Env `VLLM_ATTENTION_BACKEND=TRITON_ATTN`
(≙ Model-Card-Flag `--attention-backend TRITON_ATTN`, hier als Env, da auf dem Image
erprobt) · `--tool-call-parser gemma4 --reasoning-parser gemma4 --enable-auto-tool-choice` ·
`--override-generation-config '{"max_new_tokens":null}'` (Diffusion) ·
`--default-chat-template-kwargs '{"enable_thinking":true}'`. NVFP4 wird i. d. R.
automatisch erkannt; sonst `--quantization modelopt` ergänzen.

**vLLM-Version:** Gemma-Diffusion braucht vLLM **≥ `0.22.1rc1.dev332`**. Daher ist das
Image für alle drei vLLM-Dienste über `.env` → **`VLLM_IMAGE`** pinnbar (statt des
rollierenden `:nightly`). Vorbelegt mit einem geprüften, reproduzierbaren Build
`vllm/vllm-openai:cu129-nightly-6607a80d…` (2026‑06‑16, **CUDA 12.9** für
SM120/Blackwell, weit über `dev332`). Neuere Tags: [hub.docker.com/r/vllm/vllm-openai/tags](https://hub.docker.com/r/vllm/vllm-openai/tags).

---

## 1b. Gemma-Reasoning: `<|channel>thought`-Leak bereinigen

Taucht beim Gemma-Modell roher Denk-Text wie `<|channel>thought …` in der Antwort
auf, ist das ein **bekannter vLLM-Bug** ([#38855](https://github.com/vllm-project/vllm/issues/38855)):
der `gemma4`-Reasoning-Parser trennt `reasoning_content` nicht, weil
`skip_special_tokens` die Kanal-Marker **vor** dem Parser entfernt. Deine Serve-Flags
sind korrekt (entsprechen dem offiziellen Gemma4-Recipe) — der echte Fix muss upstream kommen.

**1) Diagnose — zeigt das ROHE Format (bitte Output sichern):**
```bash
curl -s http://localhost:5568/v1/chat/completions -H 'Content-Type: application/json' -d '{
  "model":"main",
  "messages":[{"role":"user","content":"Nenne 3 nachhaltige ETFs. Denk kurz nach."}],
  "max_tokens":400
}' | python3 -m json.tool
```
Prüfen: hat `choices[0].message` ein Feld `reasoning`/`reasoning_content` (→ vLLM trennt
korrekt, reines OWUI-Render-Thema) **oder** stecken die `<|channel>`-Marker in `content`
(→ der Bug)?

**2) Sofort-Workaround (OWUI-Filter):** `open-webui/filters/gemma_reasoning_cleaner.py`
importieren: *OWUI → Admin → Functions → „+" → Code einfügen → aktivieren*, dann dem
Modell `main`/`gemma-main` (oder global) zuweisen. Er übersetzt
`<|channel>thought … <channel|>` in natives `<think>…</think>` (einklappbares „Thinking")
bzw. entfernt die Marker.

**3) Saubere Notlösung ohne Thinking:** im Gemma-Serve-Command (docker-compose.yml)
`--default-chat-template-kwargs '{"enable_thinking":false}'` → kein Denk-Kanal, kein Leak
(dafür keine Chain-of-Thought). Vorab live testbar, indem du im curl oben
`"chat_template_kwargs":{"enable_thinking":false}` ergänzt.

> Status verfolgen: [vllm#38855](https://github.com/vllm-project/vllm/issues/38855). Sobald
> upstream gefixt, einfach ein neueres `VLLM_IMAGE`-Nightly ziehen — dann ist der Filter überflüssig.

---

## 2. Was du bekommst (claude.ai-Vergleich)

| claude.ai | Hier durch |
|---|---|
| Chat mit starkem Modell | vLLM (Qwen3.5-35B-A3B NVFP4) |
| Artifacts (HTML/SVG/JS) | OWUI Artifacts-Panel (HTML/JS/SVG, single-file React via CDN) |
| Code-Ausführung/-Korrektur | OWUI Code-Interpreter → Jupyter-Sandbox (iteriert bei Fehlern); Agent → **Microsandbox-microVM** (hardware-isoliert) |
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
├── .env.example                # → cp .env.example .env
├── start.sh  stop.sh           # Orchestrierung (Netz → RAGFlow → Stack [+Overlay])
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

./start.sh            # legt Netz an, startet RAGFlow, dann den Hauptstack
# mit Upgrade(s):  ./start.sh morphik        bzw.  ./start.sh microvm computer-use
```

`start.sh` erledigt: `docker network create aistack-rag` → RAGFlow hoch
(`./ragflow`) → Hauptstack hoch. Beim ersten Mal laden vLLM/Embeddings die
Modelle von HuggingFace (danach `HF_HUB_OFFLINE=1` in der Compose → offline).

### RAGFlow erstmalig einrichten (einmalig, ~2 Min.)
RAGFlow bringt **keine** Modelle mit — du verkabelst es mit deinem vLLM:

1. RAGFlow-UI öffnen: **http://localhost** → Konto anlegen (lokal).
2. **Model Providers** → einen **OpenAI-kompatiblen** Anbieter hinzufügen:
   - Chat-Modell: Base-URL **`http://host.docker.internal:5568/v1`**, Modell `main`, Key beliebig.
     *(`main` zeigt immer auf das aktive Hauptmodell — überlebt das Qwen⇄Gemma-Umschalten.)*
   - Embedding-Modell (**empfohlen**): den starken, multilingualen vLLM-Embedder
     eintragen — OpenAI-kompatibel, Base-URL **`http://host.docker.internal:8091/v1`**,
     Modell **`qwen3-embed`**, Dimension **2560**. *(Das ist der `vllm-embed`-Dienst,
     Qwen3-Embedding-4B; deutlich stärker als bge-m3.)*
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
docker compose --profile main-qwen up -d vllm-main   # Hauptmodell (Gemma: --profile main-gemma up -d vllm-main-gemma)
curl -s http://localhost:5568/v1/models | python -m json.tool
docker compose up -d vllm-helper embeddings qdrant searxng presidio-proxy
docker compose up -d code-sandbox agent open-webui
```

---

## 6. Open WebUI konfigurieren (http://localhost:3009)

1. **Modelle** erscheinen automatisch: `main` (= aktives Hauptmodell, Qwen *oder* Gemma),
   `qwen-helper`, `research-agent`.
2. **Task-Model** (Titel/Tags): *Admin → Settings → Interface* → **`qwen-helper`**.
3. **Code-Interpreter:** *Admin → Settings → Code Execution* → Engine **Jupyter**,
   URL `http://code-sandbox:8888`, Auth **token**, Token = dein `JUPYTER_TOKEN`.
   *(Die Compose-ENV ist ein Vorschuss; maßgeblich ist die UI — Keys variieren je OWUI-Version. [VERIFY])*
4. **Websuche:** *Admin → Settings → Web Search* → `searxng`,
   Query-URL `http://presidio-proxy:8080/search?q=<query>` → jede Suche wird PII-maskiert.
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
| *— ODER —* vllm-main-gemma (Gemma 26B-A4B NVFP4, **256k**, util 0.35, max-num-seqs 2) | ~30–34 GB |
| vllm-helper (4B, 32k, util 0.15) | ~10–12 GB |
| embeddings (bge-m3, Mem0) | ~2–3 GB |
| vllm-embed (Qwen3-Embedding-4B, FP8, util-Deckel 0.15, RAGFlow) | ~7–10 GB real |
| **Summe / frei** | **~52–58 GB / ~38 GB frei** |

> `--gpu-memory-utilization` ist nur eine **Obergrenze**, kein fixer Verbrauch — Embedding-
> Modelle füllen sie (kein KV-Cache) nicht aus. Realen Wert mit `nvidia-smi` prüfen.

> **Qwen und Gemma schließen sich aus** (Profil-Umschaltung `main-qwen`/`main-gemma`) →
> es liegt **nie mehr als ein** Hauptmodell im VRAM. `util 0.35` deckelt die
> Reservierung; da Gemma kleiner als Qwen ist, kannst du auf `0.30` senken.

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

---

## 12. [VERIFY] — vor Produktivbetrieb prüfen

- **HF-Repo** des Hauptmodells (`nvidia/Qwen3.5-35B-A3B-NVFP4` aktuell?).
- **`--quantization modelopt`**: falls vLLM meckert, `modelopt_fp4` probieren.
- **`--speculative-config` (MTP)**: nur wenn die NVFP4-Checkpoints die
  MTP-Module enthalten — sonst beim Laden Fehler → Flag entfernen.
- **Gemma-Diffusion** (`COMPOSE_PROFILES=main-gemma`): HF-Repo
  `nvidia/diffusiongemma-26B-A4B-IT-NVFP4`, die Parser `gemma4`
  (`--tool-call-parser`/`--reasoning-parser`) und `VLLM_USE_V2_MODEL_RUNNER=1`
  gegen Model-Card + dein vLLM-Nightly prüfen. Attention via Env
  `VLLM_ATTENTION_BACKEND=TRITON_ATTN` (manche Builds: CLI `--attention-backend
  TRITON_ATTN`). NVFP4 ggf. `--quantization modelopt`. Diffusionsmodelle nutzen den
  KV-Cache anders → die **autoregressiven** Sparflags (fp8-KV, chunked-prefill, MTP)
  bewusst **NICHT** übernommen; nur falls FP4-MoE unterstützt:
  `VLLM_USE_FLASHINFER_MOE_FP4=1`.
- **Vision**: KEIN `--language-model-only` (Vision-Encoder bleibt geladen).
  Falls du Bilder bewusst sperren willst: `--limit-mm-per-prompt '{"image":0}'`.
- **`--safetensors-load-strategy prefetch`**: existiert ab neueren vLLM-Builds
  (du hattest es im Einsatz) — bei älterem vLLM ggf. entfernen.
- **embeddings-Image-Tag** mit SM120-Support; **OpenAI-Route** `/v1/embeddings` von TEI für Mem0
  (sonst Mem0-Embedder auf provider `huggingface` umstellen).
- **vllm-embed** (`vllm-embed`, Qwen3-Embedding-4B): Flag `--runner pooling` (aktuelles vLLM)
  vs. `--task embed` (älter) gegen dein Nightly prüfen; `/v1/embeddings` testen:
  `curl -s localhost:8091/v1/embeddings -H 'Content-Type: application/json' -d '{"model":"qwen3-embed","input":"hallo welt"}' | head -c 200`.
  `--quantization fp8` notfalls weglassen oder vorquantisierten FP8/AWQ-Checkpoint nutzen.
  In RAGFlow die **Dimension 2560** (4B) bzw. 4096 (8B) eintragen.
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
docker compose logs -f vllm-main-gemma    # Gemma-Hauptmodell (COMPOSE_PROFILES=main-gemma)
# Hauptmodell umschalten: in .env COMPOSE_PROFILES=main-qwen|main-gemma setzen -> ./start.sh
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
> cp -f searxng/settings.yml searxng/runtime/settings.yml
> docker compose -f docker-compose.yml restart searxng
> ```
> Einmalig die alten, vom Container gechownten Dateien zurückholen (falls noch nötig):
> `sudo chown -R "$USER:$USER" searxng/`
