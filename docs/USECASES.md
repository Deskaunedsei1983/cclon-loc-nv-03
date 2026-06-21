# AI-Stack — Detaillierte Use-Cases

> Stand: 2026-06 · Branch `claude/gifted-carson-viw85x`
> Siehe auch: [`STACK.md`](./STACK.md) · [`FLOW.md`](./FLOW.md)

Jeder Use-Case nennt **Ziel**, **Voraussetzungen** (Profile/`.env`), **Ablauf**
(welche Dienste, Schritt für Schritt), **Ergebnis** und **Stolpersteine**.

---

## UC-1 — Fachfrage mit RAG + belegter Web-Gegenprüfung

**Ziel:** Frage zur Sozialversicherungs-Domäne; Antwort aus eigenen Dokumenten,
gegen das Web quantifiziert geprüft.

**Voraussetzungen:** RAGFlow befüllt; `AGENT_IMPL=langgraph`; Trust-Liste passend
(z. B. `*.gv.at trusted recht,sozialversicherung`).

**Ablauf:**
1. OWUI → `research-agent`: *„Wie hoch ist die Höchstbeitragsgrundlage ASVG 2026?"*
2. **gather**: RAGFlow liefert die internen Chunks.
3. **draft**: das Hauptmodell formuliert die Antwort **nur** aus den Belegen, mit dem
   echten Datum als Anker.
4. **verify**: erzeugt eine PII-freie Query (z. B. *„Höchstbeitragsgrundlage ASVG 2026"*)
   → Presidio → searxng → Treffer mit Domains + Trust-Tags.
5. **critic**: prüft, ob die Web-Befunde eingearbeitet sind → ggf. `REVISE`.

**Ergebnis:** Antwort + Block, z. B.:
```
Höchstbeitragsgrundlage ASVG 2026: …
  RAG: 100% (2/2 | ASVG-Leitfaden.pdf)
  Web: 80% (4/5, davon 3 vertrauenswuerdig | sozialversicherung.at, ris.bka.gv.at, …)
  Fazit: [BESTAETIGT]
```

**Stolpersteine:** Brave/Engine-Rate-Limits → die anderen Engines liefern (Liste nicht
verkleinern). Stützt nur eine niedrige Quelle → **nicht** `[BESTAETIGT]`, sondern niedrige Konfidenz.

---

## UC-2 — Aktualitäts-/„Heute"-Frage (Datums-Anker + Ehrlichkeit)

**Ziel:** zeitkritische Frage korrekt rechnen, ohne „hat noch nicht stattgefunden"-Halluzination.

**Beispiel:** *„Wie viele Punkte kann man bei der Fußball-WM 2026 noch erreichen …?"*

**Ablauf:**
1. **draft** kennt via `now_context()` das **echte Datum** → nimmt nicht an, das Turnier
   liege in der Zukunft, sondern rechnet `Gesamt − bereits gespielt`.
2. **verify** sucht den **Live-Stand** (z. B. *„games played by 20 Juni 2026"*).
3. Quoten je Teil-Aussage:
   - „104 Spiele insgesamt" → `Web: 100% … [BESTAETIGT]`
   - „X Spiele bis heute gespielt" → falls keine belastbare Quelle: `[KEINE FUNDE]`
     (transparent als Modellwissen markiert, **kein** Fehler).

**Ergebnis:** belastbare Zahlen werden bestätigt, geschätzte ehrlich als ungeprüft
gekennzeichnet — statt einer selbstbewusst falschen Zahl.

**Stolpersteine:** ohne erreichbare Live-Quelle bleibt die Restzahl `[KEINE FUNDE]` —
gewollt. Härtung: zuverlässige Quelle (Wikipedia/offiziell) in die Trust-Liste.

---

## UC-3 — Berechnung / Datei erzeugen (luftdichte Sandbox)

**Ziel:** Rechnen oder eine Office-Datei (.xlsx/.docx/.pptx) / Notebook erzeugen.

**Ablauf:**
1. Frage z. B.: *„Erzeuge eine Excel mit den Monatsbeiträgen 2026."*
2. **draft** gibt **genau einen** ` ```python `-Block aus.
3. **execute** läuft in `code-sandbox` (Netz `aistack-sandbox`, **kein Egress**) mit
   polars/openpyxl/python-docx etc.
4. Ergebnis/Dateiname fließt in die Antwort.

**Ergebnis:** geprüftes Rechenergebnis bzw. erzeugte Datei — der Agent behauptet nie
ein Ergebnis ohne Ausführung.

**Stolpersteine:** Code kann **nichts** ins Internet schicken (Designziel). Für stärkere
Isolierung: Profil `microvm` (microVM-Executor).

---

## UC-4 — Dokument-Upload (Text → RAGFlow, Scan/Bild → Morphik)

**Ziel:** eigene Dokumente durchsuchbar machen, automatisch ans richtige Backend.

**Voraussetzungen:** für Scans/Bilder Profil `morphik`; Dimensions-Konsistenz
(`vllm-embed` ↔ `morphik.toml`/`VECTOR_DIMENSIONS`, 4B = 2560).

**Ablauf:**
1. Upload → **ingest-router** klassifiziert:
   - PDF mit Tabellen/Diagrammen/Scan → **Morphik** (ColPali-Multivektoren, lokal).
   - reiner Text → **RAGFlow** (Chunks + `qwen3-embed`).
2. Im Chat werden die Inhalte über **gather** mitgenutzt (UC-1).

**Ergebnis:** Bild-/Layout-lastige Dokumente werden visuell, Text klassisch indexiert.

**Stolpersteine:** Dimensionswechsel des Embedders ⇒ RAGFlow-KB **neu** und Morphiks
`VECTOR_DIMENSIONS` angleichen, sonst „dimension mismatch".

---

## UC-5 — Gedächtnis über Sitzungen (Mem0)

**Ziel:** der Agent erinnert sich an persönliche Fakten/Präferenzen.

**Voraussetzungen:** `MEM0_ENABLED=true`; ein erreichbares mem0-LLM (Selektor `auto`).

**Ablauf:**
1. Nutzer: *„Merk dir: ich heiße Stefan, arbeite in der SV und nutze gern Pandas."*
2. **mem_add** speichert (siehe FLOW-5):
   - `MEM0_INFER=false`: die Aussage wird **direkt** eingebettet/gespeichert (robust).
   - `MEM0_INFER=true`: destillierte Fakten („Name is Stefan", …) — braucht ein
     zuverlässiges JSON-LLM (`main-qwen-plain`).
3. Spätere Sitzung: **mem_search** holt relevante Erinnerungen in den Kontext.

**Ergebnis:** personalisierte Antworten; `Memories`-Count in Qdrant steigt.

**Stolpersteine:** `infer=true` mit **MTP-Qwen** oder **Gemma** liefert kaputtes/leeres
JSON → bei `infer=false` bleiben **oder** `main-qwen-plain` nutzen (UC-6).

---

## UC-6 — VRAM-Tuning & Modellwechsel (die drei Hebel)

**Ziel:** den Stack auf knappes VRAM trimmen bzw. Modelle umschalten — alles per `.env`.

**Hebel:**
1. **Hauptmodell** (`COMPOSE_PROFILES`, genau eines):
   - `main-qwen` — schnellstes Chat (MTP).
   - `main-qwen-plain` — ohne MTP → **sauberes JSON** für `mem0 infer=true`.
   - `main-gemma` — Diffusion, multimodal.
2. **GPU-Helfer** weglassen (kein `helper` im Profil) → spart VRAM. mem0/Tasks laufen
   auf dem **CPU**-Helfer.
3. **Embedder** verschlanken: `EMBED_MODEL=Qwen/Qwen3-Embedding-4B` (`0.15` GPU-Util).

**Ablauf (Beispiel „maximal schlank"):**
```ini
COMPOSE_PROFILES=main-qwen,mem0struct      # kein helper
MEM0_INFER=false
EMBED_MODEL=Qwen/Qwen3-Embedding-4B
```
→ altes `vllm_main*` stoppen → `./start.sh` (serielles Laden).

**Ergebnis:** ein Hauptmodell + CPU-Helfer (~0 VRAM) + schlanker Embedder; mem0 stabil.

**Stolpersteine:** Profil wechseln entfernt den alten Container nicht automatisch →
`docker rm -f vllm_main vllm_main_gemma` vor dem Neustart (Port 5568/VRAM).

---

## UC-7 — DSGVO / PII-Härtung im Betrieb

**Ziel:** sicherstellen, dass keine personenbezogenen Daten ungewollt nach außen gehen.

**Maßnahmen im Stack:**
- **Suche maskiert**: Such-Queries gehen nur PII-frei + zusätzlich Presidio-maskiert ans Web.
- **Sandbox luftdicht**: generierter Code (Netz `aistack-sandbox`) hat **keinen** Egress.
- **Egress isoliert**: die Auto-Blocklist holt ein eigener Sidecar **ohne** Zugriff auf
  Stack/Userdaten — der Agent selbst macht keinen Outbound-Request.
- **Lokale Inferenz**: Modelle laufen lokal; Egress nur für den **einmaligen** Modell-Download.

**Prüfen:** Grafana-Dashboard „Container-Logs & Fehler" (Websuche-Pfad: Presidio-Treffer,
searxng, OWUI). `docker exec agent …` für gezielte Checks.

**Stolpersteine:** Auto-Blocklist braucht Egress **nur** im Sidecar (`BLOCKLIST_URL` +
Profil `blocklist`) — bewusst opt-in.

---

## UC-8 — Vertrauenswürdigkeit der Web-Belege steuern (Trust-Liste)

**Ziel:** festlegen, welche Quellen „voll zählen".

**Ablauf:**
1. `agent/trust_domains.txt` pflegen:
   ```
   *.gv.at            trusted   recht,verwaltung
   sozialversicherung.at trusted sozialversicherung
   *.reddit.com       low
   ```
2. Schnell-Ergänzung ohne Datei-Edit: `.env` `TRUST_DOMAINS=…` / `LOW_TRUST_DOMAINS=…`.
3. `docker compose up -d --force-recreate agent`.

**Wirkung:** in der Web-Quote erscheint „… davon t vertrauenswuerdig"; `[BESTAETIGT]`
erfordert vertrauenswürdige (themen-passende) Stütze. Eine `trusted·recht`-Quelle
zählt bei einer Technik-Frage nur wie neutral.

**Stolpersteine:** das ist **kein** automatisches Trust-Ranking — die Qualität hängt an
der gepflegten Liste (für die SV-Domäne genau richtig: du bestimmst die Autoritäten).
