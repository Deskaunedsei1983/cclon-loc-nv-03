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

# --- SearXNG-Settings in gitignore-ten Runtime-Ordner spiegeln ---------------
#  Der searxng-Container (UID 977) patcht settings.yml beim Start per 'sed -i'
#  und wuerde so die GIT-Datei umschreiben/chownen -> danach braucht 'git pull'
#  sudo. Loesung: der Container mountet ./searxng/runtime (gitignored); die
#  git-getrackte ./searxng/settings.yml wird nur HINEINKOPIERT (cp -f kommt ohne
#  sudo aus, da das runtime-Verzeichnis dem User gehoert).
if [ -f searxng/settings.yml ]; then
  mkdir -p searxng/runtime
  cp -f searxng/settings.yml searxng/runtime/settings.yml
  echo "   + searxng/settings.yml -> searxng/runtime/ gespiegelt"
fi

COMPOSE_FILES=(-f docker-compose.yml)

# --- Zentrales Logging/Observability (Loki+Grafana+Dozzle+Promtail) ----------
#  Standardmaessig AN -> ALLE Container-Logs laufen zentral in Grafana/Loki
#  zusammen (+ Dozzle-Live-Viewer). Opt-out:  LOGGING_STACK=0 ./start.sh
LOGGING_STACK="${LOGGING_STACK:-1}"
if [ "$LOGGING_STACK" = "1" ]; then
  COMPOSE_FILES+=(-f docker-compose.observability.yml)
  echo "   + Observability AN -> Grafana :${GRAFANA_PORT:-3011}, Dozzle :${DOZZLE_PORT:-8085} (aus: LOGGING_STACK=0)"
fi

# --- Hauptmodell-Profil robust aus .env (COMPOSE_PROFILES) bestimmen --------
#  Genau EIN Hauptmodell (main-qwen|main-gemma). Wird IMMER explizit per
#  --profile uebergeben -> egal ob die Compose-Version COMPOSE_PROFILES und
#  --profile vereinigt oder ueberschreibt, das Hauptmodell startet zuverlaessig.
ENV_PROFILES=""
[ -f .env ] && ENV_PROFILES="$(grep -E '^[[:space:]]*COMPOSE_PROFILES=' .env | tail -n1 | cut -d= -f2- | tr -d "\"' ")"
has_qwen=0; has_qwen_plain=0; has_gemma=0
case ",$ENV_PROFILES," in *,main-qwen,*)       has_qwen=1       ;; esac
case ",$ENV_PROFILES," in *,main-qwen-plain,*) has_qwen_plain=1 ;; esac
case ",$ENV_PROFILES," in *,main-gemma,*)      has_gemma=1      ;; esac
if [ $((has_qwen + has_qwen_plain + has_gemma)) -gt 1 ]; then
  echo "FEHLER: Mehr als EIN Hauptmodell in COMPOSE_PROFILES"
  echo "        (main-qwen | main-qwen-plain | main-gemma). Genau EINES waehlen. Abbruch."
  exit 1
fi
MAIN_PROFILE="main-qwen"
[ "$has_qwen_plain" = 1 ] && MAIN_PROFILE="main-qwen-plain"
[ "$has_gemma" = 1 ] && MAIN_PROFILE="main-gemma"
[ $((has_qwen + has_qwen_plain + has_gemma)) -eq 0 ] && \
  echo "   ! Kein Hauptmodell in .env (COMPOSE_PROFILES) -> Default: main-qwen"
echo "   + Hauptmodell-Profil: $MAIN_PROFILE"
MAIN_PROFILE_ARGS=(--profile "$MAIN_PROFILE")

# --- Upgrade-Profile (per CLI, z.B. ./start.sh morphik) ---------------------
PROFILE_ARGS=()
for p in "${PROFILES[@]:-}"; do
  [ -n "$p" ] || continue
  PROFILE_ARGS+=(--profile "$p")
done
if [ "${#PROFILE_ARGS[@]}" -gt 0 ]; then
  echo "   + Upgrade-Overlay aktiv: ${PROFILES[*]}"
  COMPOSE_FILES+=(-f docker-compose.upgrades.yml)
fi
dc() { docker compose "${COMPOSE_FILES[@]}" "${MAIN_PROFILE_ARGS[@]}" "${PROFILE_ARGS[@]}" "$@"; }
main_up() { dc up -d --build; }

# --- Serielles GPU-Modell-Laden (gegen OOM durch gestapelte Lade-Peaks) -------
#  vLLM laedt bei Online-Quantisierung zuerst die FP16-Gewichte -> kurzer VRAM-
#  Peak, DANN wird quantisiert (kleiner). Starten alle GPU-Dienste gleichzeitig,
#  stapeln sich diese Peaks -> OOM beim Init, obwohl der Dauerzustand passt.
#  Loesung: nacheinander hochfahren, jeder erst wenn der vorige /health meldet
#  (= fertig geladen + quantisiert). Abschalten:  SERIAL_GPU_LOAD=0 ./start.sh
SERIAL_GPU_LOAD="${SERIAL_GPU_LOAD:-1}"

http_ok() {  # 200-Check, tolerant ggue. fehlendem curl
  if   command -v curl >/dev/null 2>&1; then curl -fsS -o /dev/null "$1" 2>/dev/null
  elif command -v wget >/dev/null 2>&1; then wget -q -O /dev/null "$1" 2>/dev/null
  else python3 -c "import sys,urllib.request; urllib.request.urlopen(sys.argv[1],timeout=5)" "$1" 2>/dev/null
  fi
}

wait_health() {  # wait_health <url> <name> <max_min>
  local url="$1" name="$2" max="${3:-20}"; local tries=$(( max * 4 )) i=0
  echo "   ... warte auf $name ($url) bis zu ${max} min"
  until http_ok "$url"; do
    i=$((i+1))
    if [ "$i" -ge "$tries" ]; then
      echo "   ! $name nach ${max} min nicht bereit -> fahre trotzdem fort"; return 1
    fi
    sleep 15
  done
  echo "   + $name bereit."
}

if [ "$SERIAL_GPU_LOAD" = "1" ]; then
  echo ">> Serielles GPU-Laden AN (kein gestapelter Lade-Peak). Aus: SERIAL_GPU_LOAD=0 ./start.sh"
  MAIN_SVC="vllm-main"
  [ "$has_qwen_plain" = 1 ] && MAIN_SVC="vllm-main-qwen-plain"
  [ "$has_gemma" = 1 ] && MAIN_SVC="vllm-main-gemma"
  echo ">> [seriell 1/4] Hauptmodell ($MAIN_SVC)"
  retry 3 dc up -d --no-deps "$MAIN_SVC";  wait_health "http://localhost:5568/health" "vLLM main" 30
  echo ">> [seriell 2/4] Mem0-Embedder (TEI bge-m3)"
  retry 3 dc up -d --no-deps embeddings;   wait_health "http://localhost:8082/health" "TEI bge-m3" 15
  echo ">> [seriell 3/4] RAGFlow/Morphik-Embedder (vllm-embed)"
  retry 3 dc up -d --no-deps vllm-embed;   wait_health "http://localhost:8091/health" "vLLM embed" 20
  case ",$ENV_PROFILES," in
    *,helper,*)
      echo ">> [seriell 4/4] Helfer (vllm-helper)"
      retry 3 dc --profile helper up -d --no-deps vllm-helper
      wait_health "http://localhost:30001/health" "vLLM helper" 15 ;;
    *) echo "   (Helfer aus -> uebersprungen)";;
  esac
  echo ">> GPU-Modelle geladen -> jetzt den Rest (morphik/ColPali laedt nun allein)"
fi

retry 3 main_up

echo "================================================================"
echo ">> Endzustand RAGFlow:"
( cd ragflow && docker compose ps ) 2>&1 || true
echo ">> Endzustand Kernstack:"
docker compose "${COMPOSE_FILES[@]}" "${MAIN_PROFILE_ARGS[@]}" "${PROFILE_ARGS[@]}" ps 2>&1 || true
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
  - Grafana (Logs) : http://localhost:${GRAFANA_PORT:-3011}   Dashboard "AI-Stack — Container-Logs & Fehler"
  - Dozzle (live)  : http://localhost:${DOZZLE_PORT:-8085}    Live-Logs aller Container

Wo schauen, wenn etwas klemmt (z.B. Websuche)?
  -> Grafana oeffnen, Dashboard "AI-Stack — Container-Logs & Fehler".
     Panel "Fehler & Warnungen" zeigt sofort den schuldigen Container; das
     "Websuche-Pfad"-Panel zeigt presidio (Treffer-Anzahl!) + searxng + OWUI.

Bei Problemen diese Logdatei teilen: $LOG
EOF
