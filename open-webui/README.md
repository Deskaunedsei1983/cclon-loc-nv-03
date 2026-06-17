# Open WebUI — versionierte Konfiguration

OWUI speichert System-Prompt, Skills und Functions in seiner **eigenen DB** (Volume
`open-webui-data`). Diese Dateien sind die **versionierte Quelle der Wahrheit**, damit
nichts verloren geht und wir Änderungen im Branch nachvollziehen/anpassen können.

| Datei | Was | Anwenden in OWUI |
|---|---|---|
| `system-prompt.md` | System-Prompt fürs Haupt-Modell (`main`/`gemma-main`) | Workspace → Models → `main` → *System Prompt* einfügen |
| `skills/*.md` | Die 3 Skills (editierbare Einzeldateien) | Quelle zum Bearbeiten |
| `skills/skills-export.json` | Dieselben 3 Skills, **re-importierbar** | Admin → Skills → *Import* |
| `filters/gemma_reasoning_cleaner.py` | OWUI-Filter gegen den Gemma-`<\|channel>`-Leak | Admin → Functions → *Import/New* |

## Workflow
1. **Bearbeiten:** die `.md`/`.py` hier ändern.
2. **JSON neu bauen** (hält `skills-export.json` synchron zu den `.md`):
   ```bash
   cd open-webui/skills && python3 - <<'PY'
   import json
   meta=[("content-and-document-intelligence","Content & Document Intelligence Engine",
          "Analysiert, vergleicht und extrahiert geschäftliche Dokumente, E-Mails (.eml) und führt strukturierte Konkurrenzanalysen durch."),
         ("data-intelligence-and-analytics","Data Intelligence & Analytics Engine",
          "Verarbeitet massive Datenbestände, führt relationale SQL-Analysen durch und erstellt automatisierte Finanz- und Daten-Reports inklusive Charts."),
         ("software-and-app-architect","Software & App Architect Engine",
          "Baut vollständige Software-Projekte (FastAPI, Vue.js, Tailwind, DuckDB) und führt tiefgehende Code-Reviews durch.")]
   json.dump([{"id":i,"name":n,"description":d,"content":open(f"{i}.md",encoding="utf-8").read(),
               "meta":{"tags":[]},"is_active":True,"access_grants":[]} for i,n,d in meta],
             open("skills-export.json","w",encoding="utf-8"),ensure_ascii=False,indent=2)
   PY
   ```
3. **In OWUI übernehmen:** System-Prompt einfügen bzw. `skills-export.json` importieren.

---

## ⚠️ Websuche „No sources found" trotz Treffern — PersistentConfig-Falle

Viele OWUI-Settings (u. a. die Web-Search-Bypässe) sind **`PersistentConfig`**: Die ENV in
der `docker-compose.yml` wird **nur beim ALLERERSTEN Start** in OWUIs DB geschrieben.
Lief OWUI schon, **gewinnt der DB-Wert** und die ENV wird ignoriert. Deshalb wirken
`BYPASS_WEB_SEARCH_*` aus der Compose bei einer bestehenden Instanz **nicht** — man muss
sie in der **Admin-UI** umlegen:

- **Admin → Settings → Documents → „Bypass Embedding and Retrieval" = AN**
  (sonst verwirft OWUIs RAG-Filter die Web-Treffer → „No sources found").
- **Admin → Settings → Web Search → „Bypass Web Loader" = AN**
  (nutzt die SearXNG-Snippets direkt statt langsamem/blockiertem Seiten-Fetchen).

Alternative „Compose = Quelle der Wahrheit": `ENABLE_PERSISTENT_CONFIG=False` im
`open-webui`-ENV setzen → dann gelten die ENV-Werte wieder, **aber** UI-Änderungen an
diesen Infra-Settings werden ignoriert (Prompt/Skills/Models bleiben unberührt).

