# AI-Stack — Abläufe & Datenflüsse

> Stand: 2026-06 · Branch `claude/gifted-carson-viw85x`
> Siehe auch: [`STACK.md`](./STACK.md) · [`USECASES.md`](./USECASES.md)

Dieses Dokument beschreibt die **Laufzeit-Pfade** durch den Stack — was wann wohin
fließt. Legende: `►` Aufruf/Datenfluss · `▣` Persistenz · `⟲` Schleife.

---

## 1. Start-Flow (`./start.sh`)

```
./start.sh
  │
  ├─[1] Netz 'aistack-rag' sicherstellen
  ├─[2] RAGFlow hochfahren            (./ragflow: ES · MySQL · MinIO · Redis · ragflow-server)
  ├─[3] Hauptmodell-Profil aus .env bestimmen   (genau EIN main-* ; sonst Abbruch)
  │
  └─[4] SERIELLES GPU-Laden (SERIAL_GPU_LOAD=1)  — jeweils warten auf /health:
        vllm-main(-nemotron|-qwen|-plain) ►/health
          └► embeddings (TEI bge-m3)    ►/health
               └► vllm-embed            ►/health
                    └► vllm-helper*      ►/health      (* nur wenn Profil 'helper')
        danach: docker compose up -d --build  (leichte Dienste + Agent + OWUI;
                morphik/ColPali lädt – falls Profil – zuletzt allein)
```
**Warum seriell?** vLLM lädt bei Online-Quantisierung erst FP16 (kurzer VRAM-Peak),
dann quantisiert es. Gleichzeitig gestartete Modelle stapeln diese Peaks → OOM,
obwohl der Dauerzustand passt. Seriell verhindert den gestapelten Peak.

---

## 2. Chat-Flow (Kernpfad)

```
Browser ► Open WebUI (:3009)
            │  Modell = "research-agent"
            ▼
        agent (:9000)  ─ run_agent(messages)
            │
   ┌────────┴───────────────────────────────────────────────────────────┐
   │ (A) Mem0-Recall:  bge-m3-Embedding ► Qdrant /search  ► mem_context   │
   │ (B) LangGraph-Pipeline:                                              │
   │     gather ► RAGFlow /retrieval (+ Morphik /retrieve, optional)      │
   │       └► draft   ► vllm-main /chat/completions  (datums-geankert)    │
   │            └► execute ► code-sandbox /run        (falls ```python)   │
   │                 └► verify ► (Pruefqueries) ► presidio ► searxng      │
   │                      │        ► Quoten je Aussage (RAG%/Web%/Trust)  │
   │                      └► critic ► vllm-main  ► APPROVE | REVISE ⟲     │
   │ (C) Mem0-Add:  mem_add(query[,answer]) ► (siehe Flow 5)              │
   └─────────────────────────────────────────────────────────────────────┘
            │  Antwort (+ "Web-Gegenpruefung:"-Block mit Quoten)
            ▼
        Open WebUI  ─►  Browser
                     └► Task-Modell mem0-struct: Titel/Tags/Folgefragen (Flow 6)
```
Die `pydantic`-Variante ersetzt (B) durch klassisches Tool-Calling (das Modell ruft
RAG/Web/Code als Tools auf), nutzt aber dieselben Funktionen aus `common.py`.

---

## 3. Web-Gegenprüfung & PII-Pfad (verify-Schritt)

```
draft-Aussagen
  ► _EXTRACT_SYS (LLM): max. N PII-FREIE Suchqueries  (keine Namen/VSNR/Adressen)
      ► je Query:  agent ► presidio-proxy/search?q=…   ◄── MASKIERT zusätzlich serverseitig
                            └► searxng (bing/brave/ddg/mojeek/qwant/wikipedia/wikidata)
                                 └► Treffer (Top WEB_MAX_RESULTS) je mit (Domain)
                                      └► Trust-Tagging: [vertrauenswuerdig·thema]/[niedrig]/neutral
                                           (trust_domains.txt + Auto-Blocklist + .env)
  ► _COMPARE_SYS (LLM, temp 0): pro Aussage  RAG:p% | Web:q% (davon t vertrauenswuerdig)
                                 ► Fazit [BESTAETIGT|AKTUELLER|WIDERSPRUCH|KEINE FUNDE]
```
**PII verlässt den Host nie im Klartext:** der Agent formuliert personenfrei *und*
Presidio maskiert nochmal. Treffer-Seiten lädt `browserless` (nur öffentliche URLs).

---

## 4. Ingestion-Flow (Datei-Upload)

```
Upload (OWUI-Chat oder API)
  ► ingest-router (:9010) /classify
       ├─ Bild-/Scan-/Tabellen-lastig ─► Morphik /ingest   ▣ pgvector + morphik-storage
       │                                   (ColPali-Multivektoren, lokal auf GPU)
       └─ text-lastig ────────────────► RAGFlow /ingest    ▣ ES + MinIO
                                           (Chunks, Embeddings via vllm-embed)
```
Im Chat sind die so indexierten Inhalte später über den **gather**-Schritt (Flow 2)
abrufbar.

---

## 5. Mem0-Schreib-Flow (`mem_add`)

```
mem_add(query, answer)
  │  Selektor _pick_mem0_llm():  mem0-struct ► helper ► main(autoregressiv) ► (aus)
  │
  ├─ MEM0_INFER = false (Default):
  │     speichere User-Aussage direkt:  bge-m3 ► Qdrant /upsert     ▣  (KEIN LLM-Call)
  │
  └─ MEM0_INFER = true:
        Call 1 (Extrakt):   LLM ► {"facts":[…]}            (response_format json_object)
        Call 2 (Manager):   LLM ► {"memory":[…ADD/UPDATE…]}
          ► je Fakt:  bge-m3 ► Qdrant /upsert              ▣
        (braucht zuverlässiges JSON-LLM, z. B. main-qwen-plain; MTP-Qwen scheitert)
```

---

## 6. Task-Modell-Flow (Open WebUI Hintergrundaufgaben)

```
neue/aktive Chats ► Open WebUI
  ► TASK_MODEL = "mem0-struct"  ► mem0-struct (:8088, CPU)
       ├─ Titel-Generierung
       ├─ Tag-Generierung
       ├─ Such-Query-Generierung
       └─ Folgefragen / Autocomplete
  (alles abschaltbar: OWUI_ENABLE_TASKS=false)
```
Wichtig: das ist **getrennt** vom Agenten — der `research-agent` baut seine Such-Queries
selbst (Flow 3), unabhängig von den OWUI-Toggles.

---

## 7. Auto-Blocklist-Flow (isolierter Egress)

```
blocklist-fetcher (Profil 'blocklist', KEIN internes Netz)
  ► curl BLOCKLIST_URL  (öffentliche Domainliste)
       └► schreibt ▣ Volume 'blocklist-data'  (atomar: tmp + mv, stündlich)

agent (kein eigener Egress)
  ► liest BLOCKLIST_FILE aus dem Volume (read-only)  ► merge in low-Tier
```
So bleibt **Egress auf einen Single-Purpose-Container beschränkt**, der nie Userdaten sieht.

---

## 8. Modell-Auswahl-Flow (`COMPOSE_PROFILES`)

```
.env  COMPOSE_PROFILES = main-nemotron | main-qwen | main-qwen-plain  (+ helper/mem0struct/…)
  ► start.sh: genau EIN main-* (sonst Abbruch)
  ► der gewählte vLLM-Dienst bindet Port 5568 + Netz-Alias "vllm-main"
  ► Agent/OWUI/RAGFlow rufen unverändert  http://vllm-main:5568/v1 (Modell "main")
```
Auswirkung auf mem0 (`MEM0_LLM_BASE_URL=auto`): die Reihenfolge ist `mem0-struct` (CPU)
→ `vllm-helper` → aktives Hauptmodell; das erste erreichbare gewinnt.

---

## 9. Vertrauensgrenzen (wer darf was nach außen)

| Komponente | Egress | sieht Userdaten? |
|---|---|---|
| code-sandbox | **nein** (`internal`) | ja (nur Code) — kann aber nichts senden |
| agent | de-facto kein Outbound im Code (nur interne Dienste) | ja |
| presidio-proxy → searxng → Web | ja (Such-Queries) | **maskiert** |
| browserless | ja (öffentliche Treffer-URLs) | nein (nur URLs) |
| blocklist-fetcher | ja (eine öffentliche Liste) | **nein** |
| vLLM-/Embed-/llama.cpp-Dienste | nur einmaliger Modell-Download | ja (lokale Inferenz) |
