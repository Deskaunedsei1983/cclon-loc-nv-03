#!/usr/bin/env bash
# ============================================================================
#  Stoppt ALLES: Kernstack + Observability + saemtliche optionalen Profile +
#  RAGFlow. Protokolliert nach ./logs/.
#
#    ./stop.sh              alles stoppen (Container weg, Daten bleiben)
#    ./stop.sh --volumes    zusaetzlich die VOLUMES loeschen (DATENVERLUST!)
#
#  Hinweis: 'docker compose down' entfernt NUR Container aktivierter Profile —
#  darum werden hier ALLE Profile aufgezaehlt (sonst bliebe z.B. mem0-struct
#  oder vllm-helper laufen). Bei neuem Profil: HIER ergaenzen (wie in start.sh).
# ============================================================================
set -uo pipefail
cd "$(dirname "$0")"

ALL_PROFILES=(
  main-qwen main-qwen-plain main-nemotron   # Hauptmodelle (alle, damit auch alte weg sind)
  helper mem0struct blocklist               # Kern-Optionen
  microvm computer-use morphik fragments    # Upgrade-Profile
)

LOG_DIR="./logs"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/stop_$(date +%Y%m%d_%H%M%S).log"
exec > >(tee -a "$LOG") 2>&1

echo "=== stop.sh  |  $(date -Is)  |  args: ${*:-(keine)} ==="
echo "Log: $LOG"

EXTRA=()
if [ "${1:-}" = "--volumes" ]; then
  EXTRA+=(-v)
  echo "!! --volumes: VOLUMES werden geloescht (Qdrant-Gedaechtnis, OWUI-DB, RAGFlow-Daten)"
fi

PROFILE_ARGS=()
for p in "${ALL_PROFILES[@]}"; do PROFILE_ARGS+=(--profile "$p"); done

echo ">> Kernstack + Observability + Upgrades stoppen (Profile: ${ALL_PROFILES[*]})"
docker compose -f docker-compose.yml -f docker-compose.upgrades.yml -f docker-compose.observability.yml \
  "${PROFILE_ARGS[@]}" down "${EXTRA[@]}" 2>&1 || true

echo ">> RAGFlow stoppen"
( cd ragflow && docker compose down "${EXTRA[@]}" ) 2>&1 || true

echo ">> Abschluss-Pruefung: laeuft noch etwas vom Stack?"
STACK_RE='vllm_|^agent$|code_sandbox|open-webui|searxng|presidio|qdrant|ingest_router|mem0-struct|browserless|morphik|grafana|loki|promtail|prometheus|dozzle|netdata|cadvisor|exporter|ragflow|es01|minio|redis|mysql|blocklist|fragments|microsandbox|ocu-'
LEFT="$(docker ps --format '{{.Names}}\t{{.Status}}' 2>/dev/null | grep -E "$STACK_RE" || true)"
if [ -n "$LEFT" ]; then
  echo "   ! Diese Container laufen NOCH:"
  echo "$LEFT" | sed 's/^/       /'
  echo "   -> Falls ungewollt:  docker rm -f <name>   (oder ein Profil fehlt oben in ALL_PROFILES)"
else
  echo "   [ OK ] kein Stack-Container laeuft mehr."
fi

echo ">> Ports frei? (die wichtigsten Host-Ports)"
FREE=0; BUSY=0
for p in 5568 8091 8082 6333 9009 9010 3009 8088 30001 8083 8077 8084 3010 ${GRAFANA_PORT:-3011} ${DOZZLE_PORT:-8085} ${PROMETHEUS_PORT:-9090} 9380; do
  if (command -v ss >/dev/null 2>&1 && ss -ltn 2>/dev/null | grep -q ":$p ") \
     || (command -v lsof >/dev/null 2>&1 && lsof -iTCP:"$p" -sTCP:LISTEN >/dev/null 2>&1); then
    echo "     [BELEGT] Port $p"; BUSY=$((BUSY+1))
  else
    FREE=$((FREE+1))
  fi
done
echo "   --- $FREE Ports frei, $BUSY belegt ---"

echo ">> Volumes (bleiben erhalten, ausser bei --volumes):"
docker volume ls --format '{{.Name}}' 2>/dev/null | grep -iE 'qdrant|open-webui|mem0|blocklist|morphik|loki|grafana|prometheus|ragflow|es|minio|mysql' | sed 's/^/     /' || echo "     (keine gefunden)"

echo "Gestoppt. Protokoll: $LOG"
