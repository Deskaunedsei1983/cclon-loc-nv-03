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

echo ">> Noch laufende Stack-Container? (sollte leer sein)"
docker ps --format '{{.Names}}\t{{.Status}}' \
  | grep -E 'vllm_|agent|code_sandbox|open-webui|searxng|presidio|qdrant|ingest_router|mem0-struct|browserless|morphik|grafana|loki|promtail|prometheus|dozzle|netdata|cadvisor|exporter|ragflow|es01|minio|redis|mysql' \
  || echo "   (keine) — alles gestoppt"

echo "Gestoppt. Protokoll: $LOG"
