# ingest-router — intelligente Upload-Weiche (RAGFlow ↔ Morphik)

Lädt der Nutzer im Chat eine Datei hoch, **klassifiziert** dieser Dienst sie und
leitet sie ans passende RAG-Backend weiter — ohne dass jemand etwas auswählen muss:

| Datei | Ziel | Warum |
|---|---|---|
| Bilder (png/jpg/tiff/…) | **Morphik** | visuelles/multimodales RAG (ColPali) |
| Scan-PDF (text-arm) | **Morphik** | kein extrahierbarer Text → Layout/OCR-stark |
| tabellenlastiges PDF | **Morphik** | Tabellen auf ≥ `PDF_TABLE_PAGE_RATIO` der Seiten |
| xlsx/csv/… | **Morphik**¹ | inhärent tabellarisch |
| docx/pptx/txt/md/html/eml/json | **RAGFlow** | text-/dokumentlastig |
| textbasiertes PDF | **RAGFlow** | genug extrahierbarer Text |

¹ abschaltbar via `SPREADSHEET_TO_MORPHIK=false`.

Jede Entscheidung ist **erklärbar**: Begründung steht in der Antwort (`reason`) und
im Log. Schwellen sind per ENV justierbar (siehe `.env-example`).

## Datenfluss
```
OWUI-Upload ─▶ Filter ingest_router.py ─▶ POST /ingest ─▶ classify()
              (lädt Bytes aus OWUI)                         ├─ Morphik  /ingest/file
                                                            └─ RAGFlow  /documents → /chunks (Parse)
        danach: research-agent findet die Inhalte per RAG (retrieve_documents / retrieve_multimodal)
```

## Starten
Teil des Haupt-Compose (kein Profil nötig):
```bash
docker compose up -d --build ingest-router
```
Für die Morphik-Ziele muss Morphik laufen: `docker compose --profile morphik up -d`.
Ist Morphik **aus**, lenkt der Router Bild/Tabelle automatisch auf RAGFlow um
(`INGEST_FALLBACK_TO_RAGFLOW=true`) — sonst gingen Uploads verloren.

## Konfiguration (.env)
```
RAGFLOW_API_KEY=...                 # wie beim Agent
RAGFLOW_INGEST_DATASET_ID=fe1       # Ziel-KB für Text-Uploads (MUSS gesetzt sein)
MORPHIK_API_URL=http://morphik:8000 # leer lassen => alles nach RAGFlow
PDF_TEXT_MIN_CHARS_PER_PAGE=200
PDF_TABLE_PAGE_RATIO=0.34
SPREADSHEET_TO_MORPHIK=true
```

## OWUI-Filter installieren
1. OWUI → **Admin → Functions → „+"** → Inhalt von
   `open-webui/filters/ingest_router.py` einfügen → speichern → **aktivieren**.
2. Global oder dem Modell **`research-agent`** zuweisen (Globe-Icon).
3. Fertig — **kein API-Key nötig**. Der Filter beschafft die Datei-Bytes key-los
   (Bilder als Base64 aus der Nachricht; Dokumente via `GET /files/{id}/content`
   ohne Auth, da `WEBUI_AUTH=false`; Fallbacks prozess-intern bzw. extrahierter
   Text). In den Valves ist nichts Pflicht; `owui_base_url` nur ändern, falls OWUI
   nicht auf `:8080` lauscht, `ingest_router_url` bleibt `http://ingest-router:8000`.

## Testen ohne OWUI
```bash
# nur Entscheidung (kein Upload):
curl -s -F file=@scan.pdf http://localhost:9010/classify | jq
# echt ingestieren:
curl -s -F file=@bericht.xlsx http://localhost:9010/ingest | jq
curl -s http://localhost:9010/healthz | jq
```

## [VERIFY] — versionsabhängige Stellen
- **RAGFlow-Ingest** (`app.py:_to_ragflow`): Upload `POST /api/v1/datasets/{ds}/documents`,
  Parse `POST /api/v1/datasets/{ds}/chunks {"document_ids":[…]}`. Pfade/Felder gegen
  deine RAGFlow-Version prüfen (gleiche [VERIFY]-Lage wie das Retrieval im Agent).
- **Morphik-Ingest** (`app.py:_to_morphik`): `POST /ingest/file` (multipart `file` +
  `metadata`). Endpoint/Felder gegen deine Morphik-Version prüfen.
- **OWUI-Datei-Body & key-loser Download** (`ingest_router.py`): Form von
  `body["files"]`/`messages` und `/api/v1/files/{id}/content` sind OWUI-
  versionsabhängig (hier: 0.9.5-Annahmen). Der no-auth-GET setzt `WEBUI_AUTH=false`
  voraus; läuft OWUI mit Auth, greift der prozess-interne Fallback (`open_webui`
  Files/Storage) bzw. der extrahierte Text.
