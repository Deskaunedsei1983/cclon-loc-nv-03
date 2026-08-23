#!/usr/bin/env bash
# =============================================================================
#  Datei-Browser der rechten OWUI-Seitenleiste einrichten + diagnostizieren
#  Geprueft gegen Open WebUI 0.11.0 (exakt die in docker-compose.yml gepinnte
#  Version ghcr.io/open-webui/open-webui:0.11.0).
# =============================================================================
#  Aufruf (aus dem Projektverzeichnis):
#     ./open-webui/setup-dateibrowser.sh            einrichten + pruefen
#     ./open-webui/setup-dateibrowser.sh --check    NUR pruefen, nichts aendern
#
#  Warum es dieses Skript gibt:
#   * OWUI uebernimmt TERMINAL_SERVER_CONNECTIONS aus der Umgebung NUR beim
#     allerersten Start (Config.seed_defaults ueberspringt Keys, die schon in
#     der DB stehen). Bei bestehender Installation bleibt die .env wirkungslos.
#   * Der Eintrag in den BENUTZER-Einstellungen (Integrations -> Terminals)
#     kann nicht funktionieren: dessen Verbindungstest laeuft im BROWSER
#     ("Direct connection: verify from browser"), und 'code-sandbox' ist nur
#     im Docker-Netz aufloesbar. Richtig ist der ADMIN-Bereich (serverseitiger
#     Test) — genau den schreibt dieses Skript direkt in die OWUI-Config.
#
#  Details: docs/OWUI_DATEIBROWSER.md
# =============================================================================
set -euo pipefail

cd "$(dirname "$0")/.."

CHECK_ONLY=0
[ "${1:-}" = "--check" ] && CHECK_ONLY=1

OWUI_CN="${OWUI_CONTAINER:-open-webui}"
SBX_CN="${SANDBOX_CONTAINER:-code_sandbox}"
SRV_ID="${TERMINAL_SERVER_ID:-sandbox}"
SRV_NAME="${TERMINAL_SERVER_NAME:-Chat-Dateien}"
SRV_URL="${TERMINAL_SERVER_URL:-http://code-sandbox:8000}"

fail() { echo; echo "FEHLER: $*"; exit 1; }
mask() { printf '%s' "$1" | sed -E 's/^(.{6}).*(.{4})$/\1...\2/'; }

for cn in "$OWUI_CN" "$SBX_CN"; do
  docker inspect "$cn" >/dev/null 2>&1 || fail "Container '$cn' laeuft nicht."
done

# --- 1) Token: .env gegen den TATSAECHLICH laufenden Container abgleichen -----
echo ">> [1/6] Token abgleichen (.env <-> laufender code_sandbox)"
ENV_TOKEN="${SANDBOX_FILES_TOKEN:-}"
if [ -z "$ENV_TOKEN" ] && [ -f .env ]; then
  ENV_TOKEN="$(grep -E '^[[:space:]]*SANDBOX_FILES_TOKEN=' .env | tail -n1 | cut -d= -f2- | tr -d "\"' ")"
fi
[ -n "$ENV_TOKEN" ] || fail "SANDBOX_FILES_TOKEN fehlt in der .env. Einmal ./start.sh laufen lassen."

CN_TOKEN="$(docker exec "$SBX_CN" printenv SANDBOX_FILES_TOKEN 2>/dev/null || true)"
if [ -z "$CN_TOKEN" ]; then
  fail "Der laufende Container '$SBX_CN' hat KEIN SANDBOX_FILES_TOKEN.
       Er wurde vor der .env-Aenderung gestartet. Behebung:
         docker compose up -d --build code-sandbox"
fi
if [ "$CN_TOKEN" != "$ENV_TOKEN" ]; then
  fail "Token in .env ($(mask "$ENV_TOKEN")) != Token im Container ($(mask "$CN_TOKEN")).
       Container neu erzeugen:  docker compose up -d --build code-sandbox"
fi
echo "   OK — $(mask "$ENV_TOKEN")"

# --- 2) Laeuft die Datei-API ueberhaupt? -------------------------------------
echo ">> [2/6] Datei-API im code_sandbox"
docker exec -e T="$ENV_TOKEN" "$SBX_CN" python3 - <<'PY' || fail "Datei-API antwortet nicht (siehe oben). Image neu bauen: docker compose up -d --build code-sandbox"
import json, os, sys, urllib.request
req = urllib.request.Request("http://127.0.0.1:8000/api/config",
                             headers={"Authorization": "Bearer " + os.environ["T"]})
try:
    with urllib.request.urlopen(req, timeout=8) as r:
        data = json.load(r)
except urllib.error.HTTPError as e:
    sys.exit(f"   HTTP {e.code}: {e.read()[:200]!r}")
except Exception as e:
    sys.exit(f"   {type(e).__name__}: {e}  (altes Image ohne /files-API?)")
if data.get("features", {}).get("terminal") is not False:
    sys.exit(f"   unerwartete Antwort: {data}")
print("   OK — features.terminal=false (Datei-Browser ja, Shell nein)")
PY

# --- 3) Kommt OWUI hin? (DNS + Netz + Auth) ----------------------------------
echo ">> [3/6] Erreichbarkeit AUS dem OWUI-Container ($SRV_URL)"
docker exec -e T="$ENV_TOKEN" -e U="$SRV_URL" "$OWUI_CN" python3 - <<'PY' || fail "OWUI erreicht die Sandbox nicht (Details oben)."
import json, os, socket, sys, urllib.request
url = os.environ["U"].rstrip("/")
host = url.split("//", 1)[1].split(":")[0].split("/")[0]
for var in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
    if os.environ.get(var):
        print(f"   ! WARNUNG: {var}={os.environ[var]} gesetzt — aiohttp nutzt trust_env=True.")
        print(f"     NO_PROXY muss '{host}' enthalten, sonst laeuft der Proxy-Aufruf ins Leere.")
try:
    print(f"   DNS  {host} -> {socket.gethostbyname(host)}")
except Exception as e:
    sys.exit(f"   DNS-Fehler fuer '{host}': {e}  (haengt OWUI im Netz aistack-sandbox?)")
req = urllib.request.Request(url + "/api/config",
                             headers={"Authorization": "Bearer " + os.environ["T"]})
try:
    with urllib.request.urlopen(req, timeout=8) as r:
        print(f"   HTTP {r.status} {json.load(r)}")
except urllib.error.HTTPError as e:
    sys.exit(f"   HTTP {e.code}: {e.read()[:200]!r}  (Token-Mismatch?)")
except Exception as e:
    sys.exit(f"   {type(e).__name__}: {e}")
print("   OK")
PY

# --- 4) Notebook-Kernel: Zellen ausfuehren ("Run"/"Run All" im .ipynb-Viewer) --
echo ">> [4/6] Notebook-Kernel (Zellen ausfuehren im Datei-Browser)"
docker exec -e T="$ENV_TOKEN" -e U="$SRV_URL" "$OWUI_CN" python3 - <<'PY' || \
  fail "Notebook-Kernel antwortet nicht (Details oben).
       Meist fehlt nur ein Neubau:  docker compose up -d --build code-sandbox"
import json, os, sys, urllib.error, urllib.request

BASE = os.environ["U"].rstrip("/")
TOK = os.environ["T"]
SITZUNG = "selbsttest"
NB = json.dumps({"cells": [{"cell_type": "code", "source": ["print(21*2)\n"],
                            "metadata": {}, "outputs": [], "execution_count": None}],
                 "metadata": {}, "nbformat": 4, "nbformat_minor": 5}).encode()


def ruf(pfad, daten=None, methode=None, sitzung=None, typ="application/json"):
    kopf = {"Authorization": "Bearer " + TOK}
    if sitzung:
        kopf["X-Session-Id"] = sitzung
    if daten is not None:
        kopf["Content-Type"] = typ
    r = urllib.request.Request(BASE + pfad, data=daten, headers=kopf, method=methode)
    with urllib.request.urlopen(r, timeout=120) as resp:
        roh = resp.read()
    return json.loads(roh) if roh else {}


# Testnotebook ablegen (multipart von Hand, damit keine Abhaengigkeit noetig ist)
grenze = "----selbsttest"
teil = (f"--{grenze}\r\n"
        'Content-Disposition: form-data; name="file"; filename="selbsttest.ipynb"\r\n'
        "Content-Type: application/json\r\n\r\n").encode() + NB + f"\r\n--{grenze}--\r\n".encode()
try:
    ruf("/files/upload?directory=/", teil, "POST", SITZUNG,
        f"multipart/form-data; boundary={grenze}")
except Exception as e:
    sys.exit(f"   Testnotebook nicht ablegbar: {e}")

sid = None
try:
    # GENAU wie OWUI: createNotebookSession schickt KEINE Chat-ID mit.
    s = ruf("/notebooks", json.dumps({"path": "/selbsttest.ipynb"}).encode(), "POST")
    sid = s.get("id")
    if not sid:
        sys.exit(f"   unerwartete Antwort: {s}")
    print(f"   Kernel gestartet ({s.get('kernel')})")
    e = ruf(f"/notebooks/{sid}/execute",
            json.dumps({"cell_index": 0, "source": "print(21*2)"}).encode(), "POST")
    text = "".join(o.get("text", "") for o in e.get("outputs", []))
    if "42" not in text:
        sys.exit(f"   Zelle lief, aber ohne erwartete Ausgabe: {e}")
    print("   OK — Zelle ausgefuehrt, Ausgabe korrekt (42)")
except urllib.error.HTTPError as ex:
    leib = ex.read()[:300].decode("utf-8", "replace")
    sys.exit(f"   HTTP {ex.code}: {leib}\n"
             "   404 'in keinem Chat-Ordner gefunden' -> altes Image ohne den Fallback\n"
             "   501 'Kein Kernel verfuegbar'        -> ipykernel fehlt im Image\n"
             "   -> docker compose up -d --build code-sandbox")
except Exception as ex:
    sys.exit(f"   {type(ex).__name__}: {ex}")
finally:
    try:
        if sid:
            ruf(f"/notebooks/{sid}", None, "DELETE")
        ruf("/files/delete?path=/selbsttest.ipynb", None, "DELETE", SITZUNG)
    except Exception:
        pass
PY

# --- 5) Eintrag in OWUIs Config ----------------------------------------------
if [ "$CHECK_ONLY" = "1" ]; then
  echo ">> [5/6] Gespeicherte Terminal-Server (nur lesen)"
else
  echo ">> [5/6] Terminal-Server in die OWUI-Config schreiben"
fi
docker exec -e ID="$SRV_ID" -e NAME="$SRV_NAME" -e URL="$SRV_URL" -e T="$ENV_TOKEN" \
            -e RO="$CHECK_ONLY" "$OWUI_CN" python3 - <<'PY' || fail "Config-Schreiben fehlgeschlagen."
import json, os, sqlite3, sys, time

DB = "/app/backend/data/webui.db"
if not os.path.exists(DB):
    sys.exit(f"   {DB} nicht gefunden (andere DATABASE_URL?). Dann von Hand:\n"
             "   Admin-Bereich -> Einstellungen -> Integrations -> Terminal Servers")

KEY = "terminal_server.connections"
entry = {
    "id": os.environ["ID"],          # PFLICHT: ohne ID greift showFilesTab nicht
    "name": os.environ["NAME"],
    "url": os.environ["URL"],
    "key": os.environ["T"],
    "auth_type": "bearer",
    "enabled": True,
    "path": "/openapi.json",
}
mask = lambda s: (s[:6] + "..." + s[-4:]) if s and len(s) > 12 else ("<leer>" if not s else s)

con = sqlite3.connect(DB)
try:
    row = con.execute("SELECT value FROM config WHERE key=?", (KEY,)).fetchone()
    current = []
    if row and row[0]:
        try:
            current = json.loads(row[0]) if isinstance(row[0], str) else row[0]
        except Exception:
            current = []
    if not isinstance(current, list):
        current = []

    if os.environ["RO"] != "1":
        rest = [c for c in current if isinstance(c, dict) and c.get("id") != entry["id"]]
        current = rest + [entry]
        con.execute(
            "INSERT INTO config (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (KEY, json.dumps(current), int(time.time())),
        )
        con.commit()
finally:
    con.close()

if not current:
    sys.exit("   KEIN Terminal-Server gespeichert -> ohne --check ausfuehren.")
for c in current:
    ok = "OK  " if (c.get("id") and c.get("enabled", True)) else "PRUEF"
    print(f"   [{ok}] id={c.get('id') or '<LEER! Reiter Files bleibt aus>'} "
          f"name={c.get('name')!r} url={c.get('url')} key={mask(c.get('key',''))} "
          f"auth={c.get('auth_type')} enabled={c.get('enabled', True)}")
    if c.get("id") == os.environ["ID"] and c.get("key") != os.environ["T"]:
        print("   ! Der gespeicherte Key weicht vom SANDBOX_FILES_TOKEN ab "
              "-> ohne --check ausfuehren, das korrigiert ihn.")
PY

# --- 5) Neustart --------------------------------------------------------------
if [ "$CHECK_ONLY" = "1" ]; then
  echo ">> [6/6] --check: OWUI wird nicht neu gestartet"
else
  echo ">> [6/6] OWUI neu starten (Config wird beim Start gelesen)"
  docker restart "$OWUI_CN" >/dev/null
  echo "   OK"
fi

cat <<EOF

Noch EIN Schritt in der Oberflaeche (Open WebUI 0.11.0):

  Admin-Bereich -> Models -> research-agent  (gleiche Maske auch unter
  Workspace -> Models) -> Capabilities -> "Terminal" anhaken, darunter
  erscheint der Abschnitt "Terminal" -> "$SRV_NAME" waehlen
  -> UNTEN "Save & Update" DRUECKEN (ohne Speichern greift nichts).

Danach in einem NEUEN Chat mit diesem Modell: der Reiter "Files" erscheint
rechts (OWUI setzt meta.terminalId automatisch, Chat.svelte).

EOF
