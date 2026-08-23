# Datei-Browser in der rechten Seitenleiste (Open WebUI 0.11.0)

> **Versionsstand:** alles hier ist gegen **Open WebUI 0.11.0** geprüft — exakt
> das Image, das `docker-compose.yml` pinnt
> (`ghcr.io/open-webui/open-webui:0.11.0`). Alle Zeilenangaben stammen aus
> diesem Quelltext, nicht aus älteren Versionen oder aus der Online-Doku
> (die beschreibt einen anderen Stand). Nach einem OWUI-Upgrade gehört dieses
> Dokument gegengeprüft.

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

Der Agent schickt dieselbe Chat-ID an `POST /run` (er bekommt sie als Header,
siehe unten), und die Sandbox führt den Code **in diesem Chat-Ordner** aus.
Nebeneffekt: ein Folgelauf im selben Chat sieht die Dateien des vorherigen Laufs
— iteratives Arbeiten (erzeugen → korrigieren → ergänzen) funktioniert damit
erst richtig. Neu entstandene oder geänderte Dateien erkennt die Sandbox per
Snapshot-Vergleich (vorher/nachher), das Skript selbst liegt versteckt als
`.snippet_<id>.py` und wird nach dem Lauf gelöscht.

### Nach außen nur virtuelle Pfade

Die Datei-API spricht nach außen **nur virtuelle Pfade**: `/` ist immer der
Ordner *dieses* Chats, `/bericht.csv` eine Datei darin. Es gibt schlicht keinen
Namen für „außerhalb“ — der Browser kann den Chat-Ordner also gar nicht
verlassen, und ein Pfad aus einem anderen Chat ist nicht adressierbar.

Das ist nötig, weil `FileNav.svelte` den zuletzt betrachteten Pfad **modulweit**
speichert und über Chatwechsel hinweg mitschleppt. Mit echten Container-Pfaden
landete der Browser dadurch im Volume-Wurzelverzeichnis und zeigte
`/home/sandbox/work` samt allem, was dort lag — sichtbar am Breadcrumb, das in
`buildBreadcrumbs()` den Zweig **ohne** `fileRoot` nimmt:

```js
const parts = path.split('/').filter(Boolean);   // → "/ home / sandbox / work"
```

Alte absolute Pfade werden weiterhin angenommen (der Präfix wird abgeschnitten),
damit ein Browser-Tab mit altem Zustand nicht hängen bleibt.

Zusätzlich blendet die API `document.txt` aus (`SANDBOX_HIDE_NAMES`) — das ist
die Textfassung des Uploads für den Volltext-Modus, im Browser nur Rauschen;
die Originaldatei liegt unter ihrem echten Namen daneben.

### Stale Pfade: warum es keine 403-Sackgasse gibt

`FileNav.svelte` merkt sich den zuletzt betrachteten Pfad **modulweit**
(`<script context="module"> let savedPath`) — also über Chatwechsel hinweg. Bekommt
ein neuer Chat seine ID, schiebt FileNav diesen alten Pfad mit der **neuen**
Chat-ID an den Server:

```js
// Chat just got created (null → real ID): persist the current
// browsed path as the new session's cwd — don't re-fetch.
setCwd(terminal.url, terminal.key, savedPath, chatId);
```

Der Pfad gehört dann noch zum vorigen Chat (oder zu `_ohne_chat`). Eine strenge
Prüfung gegen den Chat-Ordner beantwortet das mit **403** — und die Seitenleiste
bleibt leer stehen. Genau das ist anfangs passiert:

```
GET  /files/list?directory=/home/sandbox/work/_ohne_chat/  403 Forbidden
POST /files/cwd                                            403 Forbidden
```

Deshalb prüft `_resolve()` jetzt in drei Stufen:

| Operation | Scope | Verhalten bei einem Pfad außerhalb des Chat-Ordners |
|---|---|---|
| `list`, `POST cwd` | `clamp` | schwenkt still auf den Chat-Ordner (auch wenn der Pfad gar nicht existiert) |
| alles andere | `session` | **403** bzw. **404** — es gibt nichts zu holen |

### Die Chat-ID kommt als HTTP-Header, nicht im Body

OWUI **entfernt** `metadata` aus dem Payload, bevor es an ein *externes*
OpenAI-kompatibles Modell geht (`routers/openai.py`:
`metadata = payload.pop('metadata', None)`). Die Chat-ID reist stattdessen als
Header:

```python
if ENABLE_FORWARD_USER_INFO_HEADERS and user:
    headers = include_user_info_headers(headers, user)
    if metadata and metadata.get('chat_id'):
        headers[FORWARD_SESSION_INFO_HEADER_CHAT_ID] = metadata.get('chat_id')
```

`FORWARD_SESSION_INFO_HEADER_CHAT_ID` ist standardmäßig `X-OpenWebUI-Chat-Id`,
und **`ENABLE_FORWARD_USER_INFO_HEADERS` steht per Default auf `False`**. Ohne
den Schalter bekommt der Agent keine Chat-ID; alles landet dann im Sammelordner
`_ohne_chat`, und der Datei-Browser zeigt unter dem Chat nichts an — obwohl die
Datei sehr wohl erzeugt wurde. Genau dieser Fall stand im Log:

```
Sandbox-Datei ueber Volume: …_v2.ipynb (294128 B) -> /sandbox-work/_ohne_chat/…
```

`docker-compose.yml` setzt den Schalter deshalb auf `true`
(`OWUI_FORWARD_USER_INFO`), und `server.py` liest den Header. Mitgesendet werden
dabei auch `X-OpenWebUI-User-Id/-Name/-Email` — die gehen ausschließlich an den
eigenen `agent`-Container im internen Netz, kein Egress. Fehlt die ID trotzdem,
schreibt der Agent eine Warnung ins Log.

### Hochgeladene Datei liegt als Original im Chat-Ordner

`document.txt` ist nur die **Textfassung** des Uploads — bei `.docx`/`.pdf` sogar
nur der extrahierte Fließtext, bei `.xlsx`/`.pptx` unbrauchbar (ein als UTF-8
dekodiertes ZIP). Wer „verbessere dieses Notebook“ sagt, braucht die echte Datei.

Deshalb legt der Agent beim ersten `run_code` einer Anfrage die **Originaldatei
unter ihrem echten Namen** in den Chat-Ordner (`_ensure_document_staged` →
`POST /files/upload`), einmal pro Anfrage und nur, wenn dort nicht schon eine
Datei gleichen Namens und gleicher Größe liegt. Der Code kann dann direkt
`json.load(open('Analyse.ipynb'))` bzw. `openpyxl`/`python-docx` benutzen, und
der Datei-Browser zeigt Original und Ergebnis nebeneinander.

Dazu kommt eine **Artefakt-Regel** im System-Prompt und ein passgenauer
`ARTEFAKT-AUFTRAG` im Request (`common.artifact_hint`): Ist das Ergebnis eine
Datei, muss sie per `run_code` geschrieben werden — der Dateiinhalt gehört
**nicht** in die Antwort. Ohne diese Regel beantwortet das Modell „überarbeite
das Notebook“ gern mit dem kompletten Notebook-JSON im Chat, und es entsteht
keine Datei zum Herunterladen.

### Seitenleiste öffnet sich von selbst

Der Filter `sandbox_files.py` sendet nach der Antwort das Event
`terminal:display_file` mit dem Sandbox-Pfad der zuletzt erzeugten Datei.
`Chat.svelte → terminalEventHandler → displayFileHandler` setzt daraufhin
`showControls` und `showFileNavPath`; `ChatControls` schaltet auf den Reiter
**Files** und `FileNav` öffnet den Ordner samt Vorschau. Abschaltbar über die
Valve `open_file_nav`.

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
> schon gestartet hat, für den bleibt die `.env` an dieser Stelle wirkungslos.

**Einfachster Weg:**

```bash
./open-webui/setup-dateibrowser.sh
```

Das Skript prüft in fünf Schritten und schreibt den Terminal-Server direkt in
OWUIs Config (idempotent, vorhandene Einträge bleiben), dann Neustart:

1. Token aus der `.env` gegen den **laufenden** `code_sandbox` abgleichen
   (häufigster Fehler: `.env` geändert, Container nicht neu erzeugt),
2. Datei-API im `code_sandbox` (`/api/config` → `features.terminal=false`),
3. Erreichbarkeit **aus dem OWUI-Container**: DNS, HTTP-Status, Proxy-Warnung,
4. Config-Eintrag schreiben und den gespeicherten Stand ausgeben,
5. OWUI neu starten.

Nur diagnostizieren, ohne etwas zu ändern:

```bash
./open-webui/setup-dateibrowser.sh --check
```

Alternativ von Hand — dann aber unbedingt so:

#### Nur im ADMIN-Bereich eintragen — nicht in den Benutzer-Einstellungen

Es gibt in OWUI **zwei** Stellen für Terminal-Server, und nur eine funktioniert
mit einem Docker-internen Hostnamen:

| Stelle | Komponente | Verbindungstest |
|---|---|---|
| **Admin-Bereich → Einstellungen → Integrations → Terminal Servers** | `admin/Settings/Integrations.svelte` (`direct = false`) | **serverseitig** über `POST /api/v1/configs/terminal_servers/verify` — der OWUI-Container fragt `http://code-sandbox:8000/api/config` ✔ |
| Einstellungen (Benutzer) → Integrations → Terminals | `chat/Settings/Integrations/Terminals.svelte` (`direct = true`) | **im Browser** (`AddTerminalServerModal.svelte`: *„Direct connection: verify from browser“*) → `code-sandbox` ist für den Browser kein auflösbarer Name ✘ |

Erkennungsmerkmal für den falschen Weg: *„Server connection failed“* **und im
`code_sandbox`-Log taucht überhaupt kein Request auf** (dort steht dann nur der
`127.0.0.1`-Aufruf der `start.sh`-Prüfung). Die Anfrage hat den Container nie
erreicht, weil sie aus dem Browser kam.

#### Felder im Admin-Dialog

* **ID**: `sandbox` — **Pflicht, obwohl das Feld „auto“ als Platzhalter zeigt.**
  Das Backend erzeugt keine ID (`TerminalServerConnection.id` bleibt `''`), und
  ohne ID greift weder `showFilesTab` (`t.id && t.id === $selectedTerminalId`)
  noch die Modell-Auswahl.
* **Name**: `Chat-Dateien`
* **URL**: `http://code-sandbox:8000` (ohne Slash am Ende)
* **Key**: der Wert von `SANDBOX_FILES_TOKEN` aus der `.env`
* **Auth**: `bearer`, **aktiviert** an

Gegenprobe aus dem OWUI-Container (muss `200` liefern):

```bash
TOKEN=$(grep -E '^SANDBOX_FILES_TOKEN=' .env | cut -d= -f2-)
docker exec open-webui python3 -c "
import urllib.request, sys
req = urllib.request.Request('http://code-sandbox:8000/api/config',
                             headers={'Authorization': 'Bearer $TOKEN'})
r = urllib.request.urlopen(req, timeout=5)
print(r.status, r.read().decode())
"
```

### 2. Modell verknüpfen

In 0.11.0 liegt die Modell-Maske an **zwei** Stellen (dieselbe Maske,
`ModelEditor.svelte`):

* **Admin-Bereich → Models → `research-agent`** ← so sieht man sie im Admin-Menü
* Workspace → Models → `research-agent`

Dort **Capabilities → „Terminal“ anhaken**; darunter erscheint der Abschnitt
**Terminal** mit einem Auswahlfeld → *Chat-Dateien* wählen → unten **„Save &
Update“ drücken**. Ohne Speichern passiert nichts.

Das schreibt `meta.terminalId` ins Modell; `Chat.svelte` setzt daraufhin bei
jedem neuen Chat automatisch `selectedTerminalId` → der Reiter **Files**
erscheint und öffnet sich von selbst (`ChatControls.svelte`).

Ohne diesen Schritt bleibt der Reiter aus, weil `showFilesTab`
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
