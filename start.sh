#!/usr/bin/env bash
# ============================================================================
#  Startet das Bundle (Netz -> RAGFlow -> Kernstack [+ optional Upgrade-Overlay]).
#  - Schreibt ALLES zusaetzlich in eine Logdatei unter ./logs/ (zum Debuggen).
#  - Wiederholt Pulls/Starts automatisch (transiente Netz-Timeouts).
#  - Bricht NICHT beim ersten Teilfehler ab -> Endzustand wird immer protokolliert.
#
#  Ohne Argumente: nur der saubere Kernstack.
#  Mit Profil(en):  ./start.sh microvm computer-use morphik
# ============================================================================
set -uo pipefail              # kein -e: Lauf soll durchlaufen + Endzustand loggen
cd "$(dirname "$0")"

PROFILES=("$@")

# --- Logging in Datei + Konsole --------------------------------------------
LOG_DIR="./logs"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/start_$(date +%Y%m%d_%H%M%S).log"
exec > >(tee -a "$LOG") 2>&1   # stdout+stderr (auch der Kindprozesse) -> Datei + Bildschirm

echo "================================================================"
echo " start.sh  |  $(date -Is)  |  Profile: ${PROFILES[*]:-(keine)}"
echo " Log:      $LOG"
echo " Host:     $(hostname)"
echo " Docker:   $(docker --version 2>/dev/null)"
echo " Compose:  $(docker compose version --short 2>/dev/null)"
echo "================================================================"

# --- Retry-Helfer (fuer flaky Pulls/Starts) --------------------------------
retry() {   # retry <versuche> <befehl...>
  local n="$1"; shift
  local i=1 rc=0
  until "$@"; do
    rc=$?
    if [ "$i" -ge "$n" ]; then
      echo "[retry] FEHLGESCHLAGEN nach $i Versuchen (rc=$rc): $*"
      return "$rc"
    fi
    echo "[retry] Versuch $i/$n fehlgeschlagen (rc=$rc) -> erneut in 15s ..."
    sleep 15; i=$((i+1))
  done
}

ragflow_up() { ( cd ragflow && docker compose up -d ); }

echo ">> [1/3] Netz 'aistack-rag' sicherstellen"
docker network inspect aistack-rag >/dev/null 2>&1 || docker network create aistack-rag

echo ">> Host-Voraussetzung fuer RAGFlows Elasticsearch:"
echo "   sudo sysctl -w vm.max_map_count=262144   (persistent: /etc/sysctl.d/)"

echo ">> [2/3] RAGFlow starten (ES/MySQL/MinIO/Redis + ragflow-server)"
chmod +x ./ragflow/entrypoint.sh 2>/dev/null || true   # +x kann beim Entpacken verloren gehen
retry 3 ragflow_up

echo ">> [3/3] Kernstack starten"
COMPOSE_FILES=(-f docker-compose.yml)
PROFILE_ARGS=()
for p in "${PROFILES[@]:-}"; do
  [ -n "$p" ] || continue
  PROFILE_ARGS+=(--profile "$p")
done
if [ "${#PROFILE_ARGS[@]}" -gt 0 ]; then
  echo "   + Upgrade-Overlay aktiv: ${PROFILES[*]}"
  COMPOSE_FILES+=(-f docker-compose.upgrades.yml)
fi
main_up() { docker compose "${COMPOSE_FILES[@]}" "${PROFILE_ARGS[@]}" up -d --build; }
retry 3 main_up

echo "================================================================"
echo ">> Endzustand RAGFlow:"
( cd ragflow && docker compose ps ) 2>&1 || true
echo ">> Endzustand Kernstack:"
docker compose "${COMPOSE_FILES[@]}" "${PROFILE_ARGS[@]}" ps 2>&1 || true
echo "================================================================"

cat <<EOF

Fertig. Vollstaendiges Protokoll: $LOG

Zugriffe:
  - Open WebUI     : http://localhost:3009
  - RAGFlow UI     : http://localhost          (Modelle + Datasets + API-Key einrichten)
  - vLLM (Haupt)   : http://localhost:5568/v1
  - vLLM (Helfer)  : http://localhost:30001/v1
  - Agent          : http://localhost:9009/v1
  - Qdrant         : http://localhost:6333

Bei Problemen diese Logdatei teilen: $LOG
EOF
