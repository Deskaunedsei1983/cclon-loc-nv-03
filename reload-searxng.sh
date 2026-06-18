#!/usr/bin/env bash
# ============================================================================
#  reload-searxng.sh — Engine-/Settings-Aenderung anwenden OHNE start.sh-Zyklus
#  ---------------------------------------------------------------------------
#  Hintergrund: Der searxng-Container liest NICHT ./searxng/settings.yml direkt,
#  sondern die gitignore-te Kopie ./searxng/runtime/settings.yml (der Container
#  patcht sie per sed -> wuerde sonst die git-Datei umschreiben). start.sh
#  spiegelt die Datei beim Hochfahren; wer searxng aber per 'docker compose
#  restart' neu startet, bekommt die alte Runtime-Kopie -> Aenderungen "wirken
#  nicht". Dieses Skript spiegelt + recreate't searxng + verifiziert live.
#
#  Nutzung:  ./reload-searxng.sh
# ============================================================================
set -uo pipefail
cd "$(dirname "$0")"

echo ">> 1/3  settings.yml -> runtime/ spiegeln"
mkdir -p searxng/runtime
# Die runtime-Datei gehoert nach Container-Start dem SearXNG-User (UID 977) und
# liegt in einem evtl. root/977-eigenen Verzeichnis -> 'cp -f' scheitert dann an
# den Rechten (genau der Bug, der die alte Config "kleben" liess). Einmalig das
# Verzeichnis zurueckholen, danach klappt das Spiegeln dauerhaft ohne sudo.
if ! cp -f searxng/settings.yml searxng/runtime/settings.yml 2>/dev/null; then
  echo "   runtime/ gehoert dem Container -> einmalig mit sudo zurueckholen ..."
  sudo chown -R "$(id -u):$(id -g)" searxng/runtime
  cp -f searxng/settings.yml searxng/runtime/settings.yml
fi
# HARTE Verifikation, dass der Spiegel wirklich die neuen Engines enthaelt
# (sonst wieder stiller Fehlschlag wie zuvor):
echo "   gespiegelter Engine-Satz in runtime/:"
grep -E "^[[:space:]]+- [a-z]+" searxng/runtime/settings.yml | sed 's/^/     /'

echo ">> 2/3  searxng neu erstellen (liest die frische Config)"
docker compose -f docker-compose.yml up -d --force-recreate --no-deps searxng

echo ">> 3/3  warten bis bereit + Live-Test ..."
for i in $(seq 1 15); do
  sleep 2
  if docker exec searxng wget -q -O /dev/null http://localhost:8080/ 2>/dev/null; then break; fi
done

echo "----- aktiver Engine-Satz IM Container -----"
docker exec searxng grep -nE "keep_only|^\s+- [a-z]+" /etc/searxng/settings.yml 2>/dev/null | head -20 || true

echo "----- Live-Test ueber den Proxy-Pfad (results sollte > 0 sein) -----"
# Exit-Code des Probes treibt das Fazit: 0 = Treffer, 1 = leer, 2 = nicht erreichbar.
if docker exec presidio_proxy python -c "
import sys, httpx
from collections import Counter
try:
    r = httpx.get('http://searxng:8080/search',
                  params={'q': 'wetter berlin', 'format': 'json'}, timeout=30)
    d = r.json(); res = d.get('results', [])
except Exception as e:
    print('Probe nicht erreichbar:', e); sys.exit(2)
print('HTTP', r.status_code, '| results', len(res))
print('unresponsive_engines:', d.get('unresponsive_engines'))
print('treffer-pro-engine:', dict(Counter(x.get('engine') for x in res)))
sys.exit(0 if res else 1)
"; then
  echo
  echo "OK: Websuche liefert Treffer -> jetzt in OWUI testen. Dass einzelne Engines"
  echo "    429/Fehler werfen (unresponsive_engines) ist normal -> die Redundanz traegt."
else
  rc=$?
  echo
  if [ "$rc" = "2" ]; then
    echo "Test-Request scheiterte (Proxy/SearXNG noch nicht bereit?) -> in ~10s manuell nachtesten."
  else
    echo "results=0 TROTZ breitem Satz -> erst JETZT ist die SERVER-IP der Verdacht"
    echo "(broad soft-block). Loesung: in .env  SEARCH_BACKEND=brave|tavily|serper|google_pse"
    echo "+ Key, dann  docker compose up -d --force-recreate presidio-proxy  (siehe .env-example)."
  fi
fi
