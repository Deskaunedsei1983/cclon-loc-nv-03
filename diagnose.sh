#!/usr/bin/env bash
# ============================================================================
#  diagnose.sh — Warum laeuft ein Dienst nicht? (nur LESEND, aendert nichts)
#
#    ./diagnose.sh            Uebersicht + Ursachen
#    ./diagnose.sh <dienst>   zusaetzlich die letzten Logzeilen dieses Dienstes
#
#  Beantwortet vor allem: haengen noch ALTE Container aus fruehereren Projekten
#  herum, die Namen oder Ports blockieren?
# ============================================================================
set -uo pipefail
cd "$(dirname "$0")"

COMPOSE_FILES=(-f docker-compose.yml -f docker-compose.observability.yml -f docker-compose.upgrades.yml)
ALL_PROFILES=(main-qwen main-qwen-plain main-nemotron helper mem0struct blocklist
              microvm computer-use morphik fragments)
PA=(); for p in "${ALL_PROFILES[@]}"; do PA+=(--profile "$p"); done

_project_name() {
  if [ -f .env ] && grep -qE '^[[:space:]]*COMPOSE_PROJECT_NAME=' .env; then
    grep -E '^[[:space:]]*COMPOSE_PROJECT_NAME=' .env | tail -n1 | cut -d= -f2- | tr -d "\"' "
  else
    basename "$PWD" | tr '[:upper:]' '[:lower:]' | tr -cd '[:alnum:]_-'
  fi
}
PROJ="$(_project_name)"

echo "================================================================"
echo " diagnose.sh | $(date -Is) | Projekt: $PROJ"
echo "================================================================"

echo
echo ">> 1) Container DIESES Projekts"
docker ps -a --filter "label=com.docker.compose.project=$PROJ" \
  --format '   {{.Names}}\t{{.State}}\t{{.Status}}' 2>/dev/null | sort || echo "   (keine)"

echo
echo ">> 2) ALTE/FREMDE Container mit unseren Namen (blockieren den Start!)"
FOUND=0
for cn in $(grep -hE '^[[:space:]]*container_name:' docker-compose.yml \
              docker-compose.observability.yml docker-compose.upgrades.yml 2>/dev/null \
            | awk '{print $2}' | tr -d '"' | sort -u); do
  docker inspect "$cn" >/dev/null 2>&1 || continue
  lbl="$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' "$cn" 2>/dev/null)"
  [ "$lbl" = "$PROJ" ] && continue
  st="$(docker inspect -f '{{.State.Status}}' "$cn" 2>/dev/null)"
  echo "   ! '$cn' gehoert zu Projekt '${lbl:-<keins>}' (Status: $st)"
  echo "     -> entfernen:  docker rm -f $cn"
  FOUND=$((FOUND+1))
done
[ "$FOUND" = 0 ] && echo "   [ OK ] keine fremden Container mit unseren Namen."

echo
echo ">> 3) Erwartete Dienste, die NICHT laufen"
want="$(docker compose "${COMPOSE_FILES[@]}" "${PA[@]}" config --services 2>/dev/null | sort -u)"
have="$(docker compose "${COMPOSE_FILES[@]}" "${PA[@]}" ps --services --status running 2>/dev/null | sort -u)"
missing="$(comm -23 <(printf '%s\n' "$want") <(printf '%s\n' "$have") 2>/dev/null)"
if [ -n "$missing" ]; then
  printf '%s\n' "$missing" | while read -r svc; do
    [ -n "$svc" ] || continue
    st="$(docker compose "${COMPOSE_FILES[@]}" "${PA[@]}" ps -a --format '{{.Service}}\t{{.Status}}' 2>/dev/null \
          | awk -v s="$svc" -F'\t' '$1==s{print $2}')"
    if [ -z "$st" ]; then
      echo "   - $svc: KEIN Container vorhanden (nie erstellt: Profil inaktiv, Build-/Pull-Fehler"
      echo "       oder Namenskonflikt) -> docker compose logs $svc"
    else
      echo "   - $svc: $st  -> docker compose logs --tail=40 $svc"
    fi
  done
else
  echo "   [ OK ] alle erwarteten Dienste laufen."
fi

echo
echo ">> 4) RAGFlow (eigenes Sub-Bundle)"
( cd ragflow 2>/dev/null && docker compose ps --format '   {{.Name}}\t{{.State}}\t{{.Status}}' 2>/dev/null ) \
  || echo "   (nicht ermittelbar)"

echo
echo ">> 5) Zuletzt beendete Container (haeufig die eigentliche Ursache)"
docker ps -a --filter status=exited --filter status=dead \
  --format '   {{.Names}}\t{{.Status}}\t{{.Image}}' 2>/dev/null | head -15 || true

echo
echo ">> 6) GPU-Auslastung (VRAM knapp? -> Dienste sterben beim Modell-Laden)"
if command -v nvidia-smi >/dev/null 2>&1; then
  nvidia-smi --query-gpu=memory.total,memory.used,memory.free --format=csv,noheader | sed 's/^/   /'
  echo "   Prozesse:"
  nvidia-smi --query-compute-apps=pid,used_memory --format=csv,noheader | sed 's/^/     /' || true
else
  echo "   (nvidia-smi nicht verfuegbar)"
fi

if [ $# -gt 0 ]; then
  echo
  echo ">> 7) Logs von '$1' (letzte 60 Zeilen)"
  docker compose "${COMPOSE_FILES[@]}" "${PA[@]}" logs --tail=60 "$1" 2>&1 \
    || docker logs --tail=60 "$1" 2>&1 || echo "   (kein Container '$1')"
fi

echo
echo "================================================================"
echo " Fertig. Haeufigste Ursachen:"
echo "  - fremder Container blockiert den Namen  -> Abschnitt 2"
echo "  - Dienst nie erstellt (Build/Pull)       -> Abschnitt 3 + docker compose logs"
echo "  - Dienst startet noch (grosses Modell)   -> nochmal pruefen, Abschnitt 5/6"
echo "================================================================"
