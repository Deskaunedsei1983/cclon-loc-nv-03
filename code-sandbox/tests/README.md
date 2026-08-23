# Tests der Sandbox-Datei-API

Prüfen `run_api.py` gegen die App im Speicher (FastAPI `TestClient`) — kein
Container, kein Netz, kein laufender Stack nötig.

```bash
python3 -m venv /tmp/sbtest && /tmp/sbtest/bin/pip install -q \
  "fastapi==0.141.1" "python-multipart==0.0.32" httpx uvicorn
for t in code-sandbox/tests/test_*.py; do /tmp/sbtest/bin/python "$t" || break; done
```

| Datei | prüft |
|---|---|
| `test_files_api.py` | Auth, Auflisten, Vorschau, Download, ZIP, Upload, Umbenennen, Löschen, Fehlerformat, kein `/files/*` im OpenAPI-Schema |
| `test_virtual.py` | **Chat-Trennung**: alle Pfade nach außen virtuell (`/` = Chat-Ordner), fremde Chats nicht adressierbar, veraltete Client-Pfade landen im richtigen Ordner statt in einer Sackgasse, `document.txt` ausgeblendet |
| `test_staging.py` | Upload-Original im Chat-Ordner, `run_code` liest es, neue Datei wird geschrieben, Original nicht als „erzeugt“ gemeldet |

Jedes Skript endet mit `ALLE TESTS BESTANDEN` / `… OK` und Exit-Code 0.
