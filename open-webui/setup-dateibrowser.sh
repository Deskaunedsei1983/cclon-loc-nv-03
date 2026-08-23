#!/usr/bin/env bash
# =============================================================================
#  Datei-Browser der rechten OWUI-Seitenleiste einrichten (ohne Klickerei)
# =============================================================================
#  Warum es dieses Skript gibt:
#   * OWUI uebernimmt TERMINAL_SERVER_CONNECTIONS aus der Umgebung NUR beim
#     allerersten Start (Config.seed_defaults ueberspringt Keys, die schon in
#     der DB stehen). Bei einer bestehenden Installation bleibt die .env wirkungslos.
#   * Der Eintrag in den BENUTZER-Einstellungen (Integrations -> Terminals)
#     funktioniert prinzipiell nicht: dessen Verbindungstest laeuft im BROWSER
#     ("Direct connection: verify from browser"), und 'code-sandbox' ist nur
#     im Docker-Netz aufloesbar. Richtig ist der ADMIN-Bereich (serverseitiger
#     Test) — genau den schreibt dieses Skript direkt in die OWUI-Config.
#
#  Aufruf (aus dem Projektverzeichnis):   ./open-webui/setup-dateibrowser.sh
#  Danach: Workspace -> Models -> research-agent -> Capabilities -> "Terminal"
#          aktivieren und den Server "Chat-Dateien" waehlen.
#  Details: docs/OWUI_DATEIBROWSER.md
# =============================================================================
set -euo pipefail

cd "$(dirname "$0")/.."

OWUI_CN="${OWUI_CONTAINER:-open-webui}"
SBX_CN="${SANDBOX_CONTAINER:-code_sandbox}"
SRV_ID="${TERMINAL_SERVER_ID:-sandbox}"
SRV_NAME="${TERMINAL_SERVER_NAME:-Chat-Dateien}"
SRV_URL="${TERMINAL_SERVER_URL:-http://code-sandbox:8000}"

TOKEN="${SANDBOX_FILES_TOKEN:-}"
if [ -z "$TOKEN" ] && [ -f .env ]; then
  TOKEN="$(grep -E '^[[:space:]]*SANDBOX_FILES_TOKEN=' .env | tail -n1 | cut -d= -f2- | tr -d "\"' ")"
fi
if [ -z "$TOKEN" ]; then
  echo "FEHLER: SANDBOX_FILES_TOKEN fehlt. Einmal ./start.sh laufen lassen (legt ihn an)"
  echo "        oder von Hand in die .env schreiben."
  exit 1
fi

for cn in "$OWUI_CN" "$SBX_CN"; do
  docker inspect "$cn" >/dev/null 2>&1 || { echo "FEHLER: Container '$cn' laeuft nicht."; exit 1; }
done

echo ">> [1/3] Erreichbarkeit aus dem OWUI-Container pruefen"
docker exec -e T="$TOKEN" -e U="$SRV_URL" "$OWUI_CN" python3 - <<'PY'
import json, os, sys, urllib.request
url = os.environ["U"].rstrip("/") + "/api/config"
req = urllib.request.Request(url, headers={"Authorization": "Bearer " + os.environ["T"]})
try:
    with urllib.request.urlopen(req, timeout=8) as r:
        data = json.load(r)
except Exception as e:
    sys.exit(f"   FEHLER: {url} nicht erreichbar/abgelehnt: {e}\n"
             "   -> Token in .env und im Container code_sandbox vergleichen:\n"
             "      docker exec code_sandbox printenv SANDBOX_FILES_TOKEN")
if data.get("features", {}).get("terminal") is not False:
    sys.exit(f"   FEHLER: unerwartete Antwort: {data}")
print("   OK — Datei-API antwortet, Shell ist aus (features.terminal=false)")
PY

echo ">> [2/3] Terminal-Server in die OWUI-Config schreiben"
docker exec -e ID="$SRV_ID" -e NAME="$SRV_NAME" -e URL="$SRV_URL" -e T="$TOKEN" "$OWUI_CN" python3 - <<'PY'
import json, os, sqlite3, sys, time

DB = "/app/backend/data/webui.db"
if not os.path.exists(DB):
    sys.exit(f"   FEHLER: {DB} nicht gefunden (andere DATABASE_URL?). Dann bitte von Hand:\n"
             "   Admin -> Einstellungen -> Integrations -> Terminal Servers")

entry = {
    "id": os.environ["ID"],          # PFLICHT: ohne ID greift showFilesTab nicht
    "name": os.environ["NAME"],
    "url": os.environ["URL"],
    "key": os.environ["T"],
    "auth_type": "bearer",
    "enabled": True,
    "path": "/openapi.json",
}

con = sqlite3.connect(DB)
try:
    row = con.execute("SELECT value FROM config WHERE key='terminal_server.connections'").fetchone()
    current = []
    if row and row[0]:
        try:
            current = json.loads(row[0]) if isinstance(row[0], str) else row[0]
        except Exception:
            current = []
    if not isinstance(current, list):
        current = []
    rest = [c for c in current if isinstance(c, dict) and c.get("id") != entry["id"]]
    new = rest + [entry]
    con.execute(
        "INSERT INTO config (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        ("terminal_server.connections", json.dumps(new), int(time.time())),
    )
    con.commit()
finally:
    con.close()
print(f"   OK — Eintrag '{entry['id']}' gespeichert ({len(new)} Terminal-Server insgesamt)")
PY

echo ">> [3/3] OWUI neu starten (liest die Config beim Start)"
docker restart "$OWUI_CN" >/dev/null
echo "   OK"

cat <<EOF

Fertig. Noch EIN Schritt in der Oberflaeche:

  Workspace -> Models -> research-agent -> Capabilities -> "Terminal" an
  und darunter den Server "$SRV_NAME" auswaehlen.

Danach setzt OWUI bei jedem Chat mit diesem Modell automatisch den Terminal
(Chat.svelte: meta.terminalId) und der Reiter "Files" erscheint rechts —
mit den Dateien DIESES Chats, Vorschau, Download und ZIP.

EOF
