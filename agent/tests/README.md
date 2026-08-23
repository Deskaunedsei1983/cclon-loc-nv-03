# Tests der Artefakt-Logik

`test_artifact_logic.py` prüft die beiden Mechanismen aus `agent/common.py`, die
dafür sorgen, dass aus „erstelle ein Jupyter-Notebook“ eine **Datei** wird und
kein Codeblock im Chat — isoliert, ohne die schweren Agent-Imports
(pydantic-ai, mem0, langgraph). Die Funktionen werden aus dem Quelltext
extrahiert und mit Stubs für die Sandbox-Anbindung ausgeführt.

```bash
python3 -m venv /tmp/sbtest && /tmp/sbtest/bin/pip install -q httpx
/tmp/sbtest/bin/python agent/tests/test_artifact_logic.py
```

Geprüft wird (`test_artifact_logic.py`):

* **`artifact_request_hint(query)`** — erkennt aus der Anfrage, dass eine Datei
  gewünscht ist (Notebook, Excel, Word, PowerPoint, PDF, CSV, Skript), und nur
  dann; „was ist ein Jupyter-Notebook?“ löst nichts aus.
* **`salvage_code_blocks(answer)`** — das Netz: hat der Agent keine Datei
  erzeugt, steht aber ein großer Code-/JSON-Block in der Antwort, wird daraus
  eine Datei im Chat-Ordner. Notebook-JSON wird am Inhalt erkannt (`"cells"` +
  `"nbformat"`), kleine Beispielblöcke bleiben inline, und wenn schon eine Datei
  entstanden ist, greift das Netz nicht.

`test_marker.py` prüft `run_files_block()`: solange die Datei-API läuft, steht
**kein** `<!--OWUI_FILES …-->`-Block mehr in der Antwort — OWUI sanitized HTML
und würde ihn als Text anzeigen. Der Dateiname bleibt sichtbar, Pfade und JSON
nicht. Ohne Datei-API (kein Token) kommt der Block als Notnagel zurück, damit
der Filter weiterhin Download-Chips bauen kann.
