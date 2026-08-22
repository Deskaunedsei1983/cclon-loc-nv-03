#!/usr/bin/env bash
# Stoppt Kernstack + Upgrades + RAGFlow. Protokolliert alles nach ./logs/.
# Volumes/Daten ebenfalls loeschen:  ./stop.sh --volumes
set -uo pipefail
cd "$(dirname "$0")"

LOG_DIR="./logs"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/stop_$(date +%Y%m%d_%H%M%S).log"
exec > >(tee -a "$LOG") 2>&1

echo "=== stop.sh  |  $(date -Is)  |  args: ${*:-(keine)} ==="
echo "Log: $LOG"

EXTRA=()
[ "${1:-}" = "--volumes" ] && EXTRA+=(-v)

echo ">> Kernstack + Upgrades + Observability stoppen"
docker compose -f docker-compose.yml -f docker-compose.upgrades.yml -f docker-compose.observability.yml \
  --profile main-qwen --profile main-qwen-plain --profile main-nemotron \
  --profile microvm --profile computer-use --profile morphik down "${EXTRA[@]}" 2>&1 || true

echo ">> RAGFlow stoppen"
( cd ragflow && docker compose down "${EXTRA[@]}" ) 2>&1 || true

echo "Gestoppt. Protokoll: $LOG"
