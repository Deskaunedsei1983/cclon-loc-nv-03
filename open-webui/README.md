# Open WebUI — versionierte Konfiguration

OWUI speichert System-Prompt, Skills und Functions in seiner **eigenen DB** (Volume
`open-webui-data`). Diese Dateien sind die **versionierte Quelle der Wahrheit**, damit
nichts verloren geht und wir Änderungen im Branch nachvollziehen/anpassen können.

| Datei | Was | Anwenden in OWUI |
|---|---|---|
| `system-prompt.md` | System-Prompt fürs Haupt-Modell (`main`/`gemma-main`) | Workspace → Models → `main` → *System Prompt* einfügen |
| `skills/*.md` | Die 3 Skills (editierbare Einzeldateien) | Quelle zum Bearbeiten |
| `skills/skills-export.json` | Dieselben 3 Skills, **re-importierbar** | Admin → Skills → *Import* |
| `filters/gemma_reasoning_cleaner.py` | OWUI-Filter gegen den Gemma-`<\|channel>`-Leak — **optional** (im Image `6607a80d`+ ist der Leak gefixt, nur Fallback für ältere) | Admin → Functions → *Import/New* |

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

## Websuche — vollautomatisch, ohne UI-Gefummel

Wir setzen **`ENABLE_PERSISTENT_CONFIG=False`** im `open-webui`-ENV → OWUI liest die
Web-Settings **aus der Compose-ENV** statt aus seiner DB. (Sonst sind viele Web-Settings
`PersistentConfig`: die ENV wird nur beim 1. Start übernommen, danach gewinnt die DB →
man müsste alles in der Admin-UI klicken.) **Modell-System-Prompt & Skills bleiben
unberührt** — die sind kein PersistentConfig.

So entsteht das „claude.ai-Feeling" — Suche liefert automatisch Tiefe, ohne Schalter:
- `WEB_SEARCH_ENGINE=searxng` + `SEARXNG_QUERY_URL` → maskierte Suche (presidio).
- OWUIs **eingebauter HTTP-Loader** (`BYPASS_WEB_SEARCH_WEB_LOADER=false`, kein
  `WEB_LOADER_ENGINE`) → **volle Seiteninhalte** (Tabellen/Zahlen), nicht nur Snippets.
  *(Playwright-Loader getestet → bricht mit OWUI 0.9.5 ab; eingebauter Loader ist robuster.
  Fallback bei Fehlern: `BYPASS_WEB_SEARCH_WEB_LOADER=true` = nur Snippets.)*
- `BYPASS_WEB_SEARCH_EMBEDDING_AND_RETRIEVAL=true` → kein RAG-Relevanzfilter, der die
  Treffer sonst verwirft („No sources found").

**Tuning** (Tempo ↔ Tiefe), alles in der `open-webui`-ENV:
| ENV | Wirkung |
|---|---|
| `WEB_SEARCH_RESULT_COUNT` | Anzahl Treffer-Seiten (mehr = tiefer, langsamer) |
| `WEB_LOADER_CONCURRENT_REQUESTS` | Seiten parallel laden |
| `BYPASS_WEB_SEARCH_WEB_LOADER` | `true` = nur Snippets (robust, flach); `false` = volle Seiten |
| `WEB_SEARCH_CONCURRENT_REQUESTS` | SearXNG-Queries parallel (niedrig lassen — presidio drosselt zusätzlich) |

> OWUI bricht Tools nach ~100 s ab. presidio-Pausen (4–7 s × Queries) + Seiten-Laden
> müssen darunter bleiben — sonst Pausen kürzen oder weniger Queries generieren lassen.

