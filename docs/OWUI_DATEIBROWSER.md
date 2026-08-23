# Datei-Browser in der rechten Seitenleiste (Open WebUI 0.11)

## Kurzfassung

**Eine Einstellung „Datei-Browser“ gibt es in Open WebUI nicht.** Der Reiter
**Files** in der rechten Seitenleiste (neben *Controls* und *Overview*) ist an
zwei Bedingungen geknüpft — nachgelesen im Quelltext von OWUI 0.11,
`src/lib/components/chat/ChatControls.svelte`:

```js
$: showFilesTab =
    ($selectedTerminalId && /* Zugriff auf diesen Terminal-Server */) ||
    (codeInterpreterEnabled && $config?.code?.interpreter_engine !== 'jupyter');
```

* **Weg (a) – Terminal-Server:** ein ausgewählter Terminal-Server
  (`selectedTerminalId`). Das ist der Datei-Teil von **Open Terminal**; er rendert
  `FileNav.svelte`. Genau darauf zielt die Doku-Seite
  `docs.openwebui.com/features/open-terminal/file-browser`.
* **Weg (b) – Pyodide:** Code-Interpreter aktiv **und** Engine ≠ `jupyter`. Dann
  rendert `PyodideFileNav.svelte` — das ist die **WASM-Ablage im Browser**
  (`/mnt/uploads` im Pyodide-Worker), also *nicht* unsere Sandbox. Für uns
  wertlos, und Terminal + Code-Interpreter schließen sich gegenseitig aus
  (`MessageInput.svelte`).

Unser Stack stand auf `CODE_INTERPRETER_ENGINE=jupyter` → **beide** Bedingungen
waren falsch, deshalb war der Reiter unsichtbar. Wir gehen Weg (a).

## Was wir gebaut haben

`code-sandbox` spielt zusätzlich zum `/run`-Endpunkt einen **Open-Terminal-Server
— aber nur den Datei-Teil**:

* `GET /api/config` meldet `{"features": {"terminal": false}}`.
  `FileNav.svelte` liest das (`terminalEnabled = config?.features?.terminal !== false`)
  und blendet die Shell aus. **Kein PTY, kein WebSocket, keine Kommandozeile** —
  die Sandbox bleibt luftdicht.
* `GET /files/list`, `/files/read`, `/files/view`, `POST /files/archive`,
  `/files/upload`, `/files/mkdir`, `/files/move`, `DELETE /files/delete`,
  `GET|POST /files/cwd`, `GET /ports`.

Damit bekommt man in der Seitenleiste: Dateiliste, Vorschau für
`docx/xlsx/pptx/pdf/ipynb/csv/md/html/Bilder`, Einzel-Download **und**
Mehrfachauswahl → ZIP.

### Pro Chat ein Ordner

`FileNav` reicht die **Chat-ID** als Header `X-Session-Id` an jeden Aufruf durch
(OWUIs Proxy leitet ihn weiter). Der Sandbox-Dienst löst daraus
`/home/sandbox/work/chat_<chat_id>/` auf und prüft jeden Pfad per `realpath`
gegen diesen Ordner. Ein Chat sieht also **nur seine eigenen Dateien** —
genau das „alle Dateien eines Chats gesammelt“.

Der Agent schickt dieselbe Chat-ID (`body.metadata.chat_id`) an `POST /run`, und
die Sandbox führt den Code **in diesem Chat-Ordner** aus. Nebeneffekt: ein
Folgelauf im selben Chat sieht die Dateien des vorherigen Laufs — iteratives
Arbeiten (erzeugen → korrigieren → ergänzen) funktioniert damit erst richtig.
Neu entstandene oder geänderte Dateien erkennt die Sandbox per Snapshot-Vergleich
(vorher/nachher), das Skript selbst liegt versteckt als `.snippet_<id>.py` und
wird nach dem Lauf gelöscht.

### Sicherheit

* Die Datei-API ist **ohne `SANDBOX_FILES_TOKEN` komplett aus** (401).
* Auth: `Authorization: Bearer <token>`, Vergleich per `hmac.compare_digest`.
* Der Browser spricht **nie** direkt mit der Sandbox: er ruft OWUI
  (`/api/v1/terminals/<id>/...`), OWUI proxyt serverseitig weiter
  (`backend/open_webui/routers/terminals.py`). Die Sandbox bleibt im
  `aistack-sandbox`-Netz ohne Egress und ohne Host-Port.
* Alle Endpunkte stehen bewusst **nicht** im OpenAPI-Schema
  (`include_in_schema=False`): OWUI lädt von Terminal-Servern sonst
  `/openapi.json` und würde dem Modell Werkzeuge wie „Datei löschen“ anbieten.
* Jeder Pfad wird gegen den Chat-Ordner geprüft; der Chat-Ordner selbst lässt
  sich nicht löschen.

## Einrichtung

### 1. Token + Server-Eintrag

`./start.sh` legt beides automatisch in der `.env` an, falls es fehlt:

```
SANDBOX_FILES_TOKEN=<32 Byte hex>
OWUI_TERMINAL_SERVERS=[{"id":"sandbox","name":"Chat-Dateien","url":"http://code-sandbox:8000","key":"<selber Token>","auth_type":"bearer","enabled":true}]
```

`OWUI_TERMINAL_SERVERS` landet als `TERMINAL_SERVER_CONNECTIONS` im
OWUI-Container.

> **Achtung bei bestehender Installation:** OWUI seedet Config-Defaults nur
> einmal — `Config.seed_defaults()` überspringt Schlüssel, die schon in der DB
> stehen („Existing DB values take precedence over defaults“). Wer OWUI vorher
> schon gestartet hat, trägt den Server stattdessen in der Oberfläche ein:
> **Admin → Einstellungen → Integrations → Terminal Servers → „+“**
> mit `URL = http://code-sandbox:8000`, `Key = SANDBOX_FILES_TOKEN`,
> `Name = Chat-Dateien`, aktiviert.

### 2. Modell verknüpfen

**Workspace → Models → `research-agent` → Capabilities → „Terminal“ an**, darunter
im Auswahlfeld den Server *Chat-Dateien* wählen. Das schreibt
`meta.terminalId` ins Modell; `Chat.svelte` setzt daraufhin bei jedem neuen Chat
automatisch `selectedTerminalId` → der Reiter **Files** erscheint und öffnet sich
von selbst (`ChatControls.svelte`).

Ohne diesen Schritt bliebe der Reiter aus, weil `showFilesTab`
`selectedTerminalId` verlangt.

### 3. Neu bauen

```bash
docker compose build code-sandbox agent
docker compose up -d code-sandbox agent open-webui
```

## Verhältnis zum Filter `sandbox_files.py`

Der Outlet-Filter (Admin → Functions) bleibt sinnvoll und stört nicht: er hängt
erzeugte Dateien zusätzlich als **echte OWUI-Dateien** an die Nachricht und
schreibt Markdown-Download-Links in die Antwort. Das ist der Weg „Download direkt
aus dem Chatverlauf“, der Datei-Browser ist der Weg „alle Dateien des Chats an
einem Ort“. Beide greifen auf dieselben Dateien zu.

## Grenzen

* **Neuer Chat:** solange OWUI noch keine Chat-ID vergeben hat, landen Dateien im
  Sammelordner `_ohne_chat`. Ab der ersten gespeicherten Nachricht stimmt die
  Zuordnung.
* **Aufräumen:** Chat-Ordner werden nicht automatisch gelöscht (das Volume
  `code-sandbox-data` wächst mit). Löschen geht direkt in der Seitenleiste oder
  über das Volume. Alte `run_*`-Ordner aus der Zeit vor den Chat-Ordnern liegen
  weiterhin im Volume, tauchen aber in keiner Seitenleiste mehr auf:
  `docker exec code_sandbox sh -c 'rm -rf /home/sandbox/work/run_*'`.
* **Vorschau-Limit:** `/files/read` liefert Text bis `SANDBOX_READ_MAX_BYTES`
  (Default 2 MB). Downloads sind unbegrenzt (Streaming).
* **Upload-Limit:** `SANDBOX_UPLOAD_MAX_BYTES` (Default 512 MB).
