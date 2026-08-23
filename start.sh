#!/usr/bin/env bash
# ============================================================================
#  Startet das Bundle (Netz -> RAGFlow -> Kernstack [+ optional Upgrade-Overlay]).
#  - Schreibt ALLES zusaetzlich in eine Logdatei unter ./logs/ (zum Debuggen).
#  - Wiederholt Pulls/Starts automatisch (transiente Netz-Timeouts).
#  - Bricht NICHT beim ersten Teilfehler ab -> Endzustand wird immer protokolliert.
#
#  Ohne Argumente: Kernstack + Observability + die Profile aus der .env.
#  Mit Profil(en):  ./start.sh microvm computer-use morphik
#  ALLES starten :  ./start.sh --all      (jedes optionale Profil + das in der .env
#                                          gewaehlte Hauptmodell)
# ============================================================================
set -uo pipefail              # kein -e: Lauf soll durchlaufen + Endzustand loggen
cd "$(dirname "$0")"

# --all = VOLLER Funktionsumfang. Enthalten sind nur Profile, die der Stack fuer
# echte Funktionen braucht — KEINE Reserve-/Wechsel-Container und nichts, was nur
# VRAM kostet, ohne etwas beizutragen:
#   mem0struct    CPU-JSON-Helfer: bedient mem0 UND OWUIs Task-Modell (~0 VRAM)
#   morphik       multimodales/visuelles RAG (ColPali laeuft lokal auf der GPU)
#   microvm       hardware-isolierter Code-Executor (Microsandbox)
#   fragments     echte React-Artifacts
# BEWUSST NICHT in --all:
#   helper                 GPU-Modell Qwen3.5-4B (~14 GB VRAM) — REDUNDANT: seine
#                          Rolle (mem0 + OWUI-Tasks) hat mem0-struct auf der CPU
#                          uebernommen. Bei Bedarf: ./start.sh --all helper
#   main-qwen/-qwen-plain  alternative HAUPTMODELLE — exklusiv, es laeuft immer nur
#                          das eine aus COMPOSE_PROFILES (sonst doppelter VRAM)
#   blocklist              Opt-in (Internet-Egress im Sidecar); wird von --all nur
#                          aktiviert, wenn BLOCKLIST_URL in der .env gesetzt ist
#   computer-use           Das Image ist eine CLAUDE-CODE-Umgebung: es verlangt
#                          ANTHROPIC_AUTH_TOKEN (Cloud-API!) und GITLAB_TOKEN und
#                          startet ohne sie seinen Dienst nicht. Das widerspricht der
#                          Stack-Vorgabe "100% lokal/DSGVO" -> nicht in --all.
#                          Bewusst trotzdem: ./start.sh computer-use
FULL_PROFILES=(mem0struct morphik microvm fragments)
# Alle bekannten optionalen Profile (Validierung/Doku):
OPTIONAL_PROFILES=(helper mem0struct blocklist morphik microvm computer-use fragments)
# Profile, die im Upgrade-Overlay (docker-compose.upgrades.yml) definiert sind:
UPGRADE_PROFILES=(microvm computer-use morphik)

START_ALL=0
PROFILES=()
for a in "$@"; do
  case "$a" in
    --all|-a) START_ALL=1 ;;
    -*) echo "Unbekannte Option: $a (erlaubt: --all)"; exit 1 ;;
    *) PROFILES+=("$a") ;;
  esac
done

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
# ragflow/.env ist gitignored (enthaelt Passwoerter/Ports) -> nach einem frischen Klon
# fehlt sie. Ohne sie ist ${RAGFLOW_IMAGE} leer und compose bricht ab mit
# "service ragflow-cpu has neither an image nor a build context specified".
if [ ! -f ragflow/.env ] && [ -f ragflow/.env-example ]; then
  cp ragflow/.env-example ragflow/.env
  echo "   + ragflow/.env aus .env-example angelegt (Passwoerter/Ports dort pruefen!)"
fi
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

# --- Datei-Browser in OWUIs rechter Seitenleiste -----------------------------
#  OWUI blendet den Reiter "Files" nur fuer einen konfigurierten TERMINAL-Server
#  ein. Unsere code-sandbox liefert dessen Datei-API (/files/*) und meldet
#  features.terminal=false -> Browser/Vorschau/Download, aber keine Shell.
#  Beide Werte muessen zusammenpassen (Bearer-Token), darum hier gemeinsam
#  erzeugt, falls sie in der .env fehlen.
if [ -f .env ]; then
  if ! grep -qE '^[[:space:]]*SANDBOX_FILES_TOKEN=[^[:space:]]' .env; then
    _sft="$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
    printf '\n# Bearer-Token zwischen OWUI und der code-sandbox-Datei-API (automatisch erzeugt)\nSANDBOX_FILES_TOKEN=%s\n' "$_sft" >> .env
    echo "   + SANDBOX_FILES_TOKEN in .env erzeugt"
  else
    _sft="$(grep -E '^[[:space:]]*SANDBOX_FILES_TOKEN=' .env | tail -n1 | cut -d= -f2- | tr -d "\"' ")"
  fi
  if ! grep -qE '^[[:space:]]*OWUI_TERMINAL_SERVERS=[^[:space:]]' .env; then
    printf '# Datei-Browser (rechte Seitenleiste) -> code-sandbox. Automatisch erzeugt.\nOWUI_TERMINAL_SERVERS=[{"id":"sandbox","name":"Chat-Dateien","url":"http://code-sandbox:8000","key":"%s","auth_type":"bearer","enabled":true}]\n' "$_sft" >> .env
    echo "   + OWUI_TERMINAL_SERVERS in .env erzeugt (Datei-Browser rechte Seitenleiste)"
    echo "     ! Bestehende OWUI-Installation: OWUI uebernimmt die Variable nur beim"
    echo "       ERSTEN Start. Sonst unter Admin -> Einstellungen -> Integrations ->"
    echo "       Terminal Servers eintragen (siehe docs/OWUI_DATEIBROWSER.md)."
  fi
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
#  Genau EIN Hauptmodell (main-nemotron|main-qwen|main-qwen-plain). Wird IMMER explizit per
#  --profile uebergeben -> egal ob die Compose-Version COMPOSE_PROFILES und
#  --profile vereinigt oder ueberschreibt, das Hauptmodell startet zuverlaessig.
ENV_PROFILES=""
[ -f .env ] && ENV_PROFILES="$(grep -E '^[[:space:]]*COMPOSE_PROFILES=' .env | tail -n1 | cut -d= -f2- | tr -d "\"' ")"
has_qwen=0; has_qwen_plain=0; has_nemotron=0
case ",$ENV_PROFILES," in *,main-qwen,*)       has_qwen=1       ;; esac
case ",$ENV_PROFILES," in *,main-qwen-plain,*) has_qwen_plain=1 ;; esac
case ",$ENV_PROFILES," in *,main-nemotron,*)   has_nemotron=1   ;; esac
if [ $((has_qwen + has_qwen_plain + has_nemotron)) -gt 1 ]; then
  echo "FEHLER: Mehr als EIN Hauptmodell in COMPOSE_PROFILES (main-nemotron |"
  echo "        main-qwen | main-qwen-plain). Genau EINES waehlen. Abbruch."
  exit 1
fi
MAIN_PROFILE="main-qwen"
[ "$has_qwen_plain" = 1 ] && MAIN_PROFILE="main-qwen-plain"
[ "$has_nemotron" = 1 ] && MAIN_PROFILE="main-nemotron"
[ $((has_qwen + has_qwen_plain + has_nemotron)) -eq 0 ] && \
  echo "   ! Kein Hauptmodell in .env (COMPOSE_PROFILES) -> Default: main-qwen"
echo "   + Hauptmodell-Profil: $MAIN_PROFILE"
MAIN_PROFILE_ARGS=(--profile "$MAIN_PROFILE")

# --- Optionale Profile: .env + CLI + --all ----------------------------------
#  WICHTIG: Die Nicht-Hauptmodell-Profile aus COMPOSE_PROFILES (mem0struct,
#  blocklist, helper ...) werden EXPLIZIT als --profile durchgereicht. Sonst haengt
#  es an der Compose-Version, ob COMPOSE_PROFILES und --profile VEREINIGT oder
#  UEBERSCHRIEBEN werden — im Ueberschreib-Fall startete z.B. mem0-struct nicht.
WANTED=()
if [ "$START_ALL" = "1" ]; then
  WANTED=("${FULL_PROFILES[@]}")
  # blocklist nur, wenn eine Quelle konfiguriert ist (sonst laeuft ein Egress-
  # Sidecar ohne Zweck) — DSGVO-Entscheidung bleibt bei der .env.
  if [ -f .env ] && grep -qE '^[[:space:]]*BLOCKLIST_URL=[^[:space:]]' .env; then
    WANTED+=(blocklist)
  else
    echo "   ! --all: 'blocklist' uebersprungen (BLOCKLIST_URL in .env leer)"
  fi
  echo "   ! --all: 'helper' NICHT gestartet (redundant zu mem0-struct, ~14 GB VRAM)."
  echo "     Trotzdem gewuenscht:  ./start.sh --all helper"
  # per CLI zusaetzlich angeforderte Profile (z.B. helper) ergaenzen
  for p in "${PROFILES[@]:-}"; do [ -n "$p" ] && WANTED+=("$p"); done
else
  # aus der .env (alles ausser den main-*-Profilen)
  IFS=',' read -r -a _envp <<< "$ENV_PROFILES"
  for p in "${_envp[@]:-}"; do
    p="$(echo "$p" | tr -d '[:space:]')"
    case "$p" in ""|main-*) continue ;; esac
    WANTED+=("$p")
  done
  # per CLI ergaenzt
  for p in "${PROFILES[@]:-}"; do [ -n "$p" ] && WANTED+=("$p"); done
fi

# deduplizieren + Argumente bauen; Upgrade-Overlay nur einbinden, wenn noetig
PROFILE_ARGS=()
NEED_UPGRADES=0
SEEN=" "
for p in "${WANTED[@]:-}"; do
  [ -n "$p" ] || continue                      # leeres Element (leeres Array + :-) ueberspringen
  case "$SEEN" in *" $p "*) continue ;; esac
  SEEN="$SEEN$p "
  PROFILE_ARGS+=(--profile "$p")
  for u in "${UPGRADE_PROFILES[@]}"; do [ "$p" = "$u" ] && NEED_UPGRADES=1; done
done
if [ "${#PROFILE_ARGS[@]}" -gt 0 ]; then
  echo "   + Optionale Profile:$SEEN"
fi
if [ "$NEED_UPGRADES" = "1" ]; then
  echo "   + Upgrade-Overlay aktiv (docker-compose.upgrades.yml)"
  COMPOSE_FILES+=(-f docker-compose.upgrades.yml)
fi
# --- Uebersicht: welche MODELLE lauft dieser Start? -------------------------
#  Macht sichtbar, was tatsaechlich GPU-Speicher belegt (haeufigste Fehlerquelle:
#  zu viele GPU-Dienste gleichzeitig -> OOM beim Laden).
_envget() { [ -f .env ] && grep -E "^[[:space:]]*$1=" .env | tail -n1 | cut -d= -f2- | sed 's/[[:space:]]*#.*//' | tr -d "\"' " ; }
# VRAM grob gegenrechnen: die --gpu-memory-utilization-Werte sind RESERVIERUNGEN
# gegen die GESAMT-VRAM. Morphik/ColPali braucht ~14 GB (Worker UND Server laden je
# eine Instanz) und bekommt sonst "CUDA out of memory".
_vram_hint() {
  local total used free
  command -v nvidia-smi >/dev/null 2>&1 || return 0
  read -r total used free < <(nvidia-smi --query-gpu=memory.total,memory.used,memory.free \
                              --format=csv,noheader,nounits | tr ',' ' ')
  echo "   GPU: ${total} MiB gesamt, ${used} MiB belegt, ${free} MiB frei (vor dem Start)"
  case "$SEEN" in *" morphik "*)
    case "$SEEN" in *" helper "*)
      echo "   ! morphik UND helper gleichzeitig: ColPali braucht ~14 GB (2 Instanzen),"
      echo "     der GPU-Helfer ~14 GB und ist REDUNDANT (mem0-struct macht dasselbe auf CPU)."
      echo "     Bei knappem VRAM zuerst 'helper' weglassen: ./start.sh --all" ;;
    esac ;;
  esac
}
_vram_hint
echo "   --- Modelle in diesem Start ---"
case "$MAIN_PROFILE" in
  main-nemotron) echo "     Hauptmodell : nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-NVFP4 + DSpark-Draft  [GPU, util $(_envget NEMOTRON_GPU_UTIL || echo 0.40)]" ;;
  main-qwen)     echo "     Hauptmodell : nvidia/Qwen3.6-35B-A3B-NVFP4 (MTP)                                 [GPU, util 0.35]" ;;
  main-qwen-plain) echo "     Hauptmodell : unsloth/Qwen3.6-27B-NVFP4 (ohne MTP)                             [GPU, util 0.35]" ;;
esac
echo "     RAG-Embedder: $(_envget EMBED_MODEL || echo 'Qwen/Qwen3-Embedding-8B')                          [GPU, util $(_envget EMBED_GPU_UTIL || echo 0.30)]  -> RAGFlow/Morphik"
echo "     Mem0-Embedder: BAAI/bge-m3 (TEI)                                                [GPU, ~2-3 GB]  -> nur Mem0"
case "$SEEN" in *" mem0struct "*) echo "     JSON-Helfer : $(_envget MEM0_STRUCT_HF || echo 'Qwen/Qwen2.5-3B-Instruct-GGUF:Q4_K_M')   [CPU, ~0 VRAM] -> mem0 + OWUI-Tasks" ;; esac
case "$SEEN" in *" helper "*)     echo "     GPU-Helfer  : Qwen/Qwen3.5-4B                                                   [GPU, util 0.15] (redundant)" ;; esac
case "$SEEN" in *" morphik "*)    echo "     ColPali     : tsystems/colqwen2.5-3b-multilingual (in morphik)                  [GPU, ~7-8 GB]" ;; esac
echo "     (OWUI nutzt fuer Chat-Uploads zusaetzlich sein eingebautes all-MiniLM-L6-v2 auf CPU)"

# --- Profile ohne fertigen Build-Kontext AUSSORTIEREN -----------------------
#  'docker compose up --build' bricht bei EINEM fehlgeschlagenen Build komplett ab
#  -> dann startet auch der Kernstack nicht. Beispiel: das fragments-Profil baut aus
#  ./fragments/app, wohin das E2B-Repo erst geklont werden muss (fragments/README.md);
#  ohne Klon fehlt package.json und 'npm install' scheitert. Solche Profile werden
#  hier VORAB entfernt, statt den ganzen Start zu reissen.
_profile_ready() {  # _profile_ready <profil> -> 0 = startbereit
  case "$1" in
    fragments)
      # Repo kann in ./fragments (direkt) ODER ./fragments/app (README-Weg) liegen.
      if   [ -f fragments/package.json ];     then export FRAGMENTS_CONTEXT=./fragments;     return 0
      elif [ -f fragments/app/package.json ]; then export FRAGMENTS_CONTEXT=./fragments/app; return 0
      fi
      echo "   ! Profil 'fragments' uebersprungen: keine package.json in ./fragments"
      echo "     oder ./fragments/app gefunden (E2B-Repo nicht geklont)."
      echo "     Einrichten:  cd fragments && git clone https://github.com/e2b-dev/fragments app"
      echo "                  cp Dockerfile app/Dockerfile   (siehe fragments/README.md)"
      return 1 ;;
    microvm)
      [ -f microsandbox-executor/Dockerfile ] && return 0
      echo "   ! Profil 'microvm' uebersprungen: microsandbox-executor/Dockerfile fehlt."; return 1 ;;
    *) return 0 ;;
  esac
}
_KEPT=(); _KEPT_SEEN=" "
for p in ${SEEN}; do
  if _profile_ready "$p"; then _KEPT+=("$p"); _KEPT_SEEN="$_KEPT_SEEN$p "; fi
done
PROFILE_ARGS=()
for p in "${_KEPT[@]:-}"; do [ -n "$p" ] && PROFILE_ARGS+=(--profile "$p"); done
SEEN="$_KEPT_SEEN"

# --- Namenskonflikte aufloesen (feste container_name) -----------------------
#  Alle Dienste haben ein festes 'container_name'. Existiert ein Container mit
#  demselben Namen aus einem ANDEREN (alten) Compose-Projekt, verweigert Docker das
#  Anlegen ("Conflict. The container name ... is already in use") — und 'compose up'
#  bricht komplett ab, d.h. auch der Kernstack startet nicht.
#  Verhalten hier bewusst konservativ:
#    - GESTOPPTE Fremd-/Altcontainer werden entfernt (risikoarm: Volumes bleiben!)
#    - LAUFENDE Container eines fremden Projekts werden NICHT angefasst, sondern
#      gemeldet — sonst wuerde dieses Skript einen anderen Stack abschiessen.
_project_name() {
  if [ -f .env ] && grep -qE '^[[:space:]]*COMPOSE_PROJECT_NAME=' .env; then
    grep -E '^[[:space:]]*COMPOSE_PROJECT_NAME=' .env | tail -n1 | cut -d= -f2- | tr -d "\"' "
  else
    basename "$PWD" | tr '[:upper:]' '[:lower:]' | tr -cd '[:alnum:]_-'
  fi
}

clear_name_conflicts() {
  local proj cn lbl state removed=0 blocked=0
  proj="$(_project_name)"
  for cn in $(grep -hE '^[[:space:]]*container_name:' docker-compose.yml \
                 docker-compose.observability.yml docker-compose.upgrades.yml 2>/dev/null \
              | awk '{print $2}' | tr -d '"' | sort -u); do
    docker inspect "$cn" >/dev/null 2>&1 || continue          # existiert nicht -> ok
    lbl="$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' "$cn" 2>/dev/null)"
    [ "$lbl" = "$proj" ] && continue                          # gehoert uns -> compose managed ihn
    state="$(docker inspect -f '{{.State.Status}}' "$cn" 2>/dev/null)"
    if [ "$state" = "running" ]; then
      echo "   ! Name '$cn' ist von einem LAUFENDEN Container des Projekts"
      echo "     '${lbl:-<ohne compose-Projekt>}' belegt -> wird NICHT automatisch entfernt."
      echo "     Wenn er weg darf:  docker rm -f $cn"
      blocked=$((blocked+1))
    else
      echo "   + entferne verwaisten Container '$cn' (Projekt '${lbl:-keins}', Status $state)"
      docker rm -f "$cn" >/dev/null 2>&1 && removed=$((removed+1))
    fi
  done
  [ "$removed" -gt 0 ] && echo "   -> $removed verwaiste Container entfernt (Volumes/Daten unberuehrt)"
  [ "$blocked" -gt 0 ] && echo "   -> $blocked Namenskonflikt(e) offen: der Start wird daran scheitern!"
  return 0
}
echo ">> Namenskonflikte pruefen (verwaiste Container aus fruehereren Laeufen)"
clear_name_conflicts

dc() { docker compose "${COMPOSE_FILES[@]}" "${MAIN_PROFILE_ARGS[@]}" "${PROFILE_ARGS[@]}" "$@"; }
main_up() { dc up -d --build; }

# --- Serielles GPU-Modell-Laden (gegen OOM durch gestapelte Lade-Peaks) -------
#  vLLM laedt bei Online-Quantisierung zuerst die FP16-Gewichte -> kurzer VRAM-
#  Peak, DANN wird quantisiert (kleiner). Starten alle GPU-Dienste gleichzeitig,
#  stapeln sich diese Peaks -> OOM beim Init, obwohl der Dauerzustand passt.
#  Loesung: nacheinander hochfahren, jeder erst wenn der vorige /health meldet
#  (= fertig geladen + quantisiert). Abschalten:  SERIAL_GPU_LOAD=0 ./start.sh
SERIAL_GPU_LOAD="${SERIAL_GPU_LOAD:-1}"

# --- Sanity-Check Hauptmodell (kohaerenter Output statt "!!!!"-Muell?) --------
#  Health=200 heisst NUR "Server oben", NICHT "Antworten sinnvoll". Manche Modell-/
#  Quant-/Kernel-Kombis auf dieser GPU (SM120: NVFP4/Marlin bzw. DeepGEMM-E8M0)
#  liefern gueltiges JSON, aber degenerierten Text. Darum EINE Testanfrage nach dem
#  Start + Heuristik -> wir sehen Muell sofort, ohne manuell zu pruefen. Aus: MAIN_SANITY_CHECK=0
MAIN_SANITY_CHECK="${MAIN_SANITY_CHECK:-1}"

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

main_sanity_check() {  # EINE Testanfrage an das Main-LLM + Muell-Heuristik
  local out rc
  out="$(python3 - <<'PY'
import json, re, sys, urllib.request
URL = "http://localhost:5568/v1/chat/completions"
payload = {"model": "main",
           "messages": [{"role": "user",
                         "content": "Antworte mit genau diesem Satz: Hallo Welt, der Stack laeuft."}],
           "max_tokens": 40, "temperature": 0,
           "chat_template_kwargs": {"enable_thinking": False}}
req = urllib.request.Request(URL, data=json.dumps(payload).encode(),
                             headers={"Content-Type": "application/json"})
try:
    with urllib.request.urlopen(req, timeout=180) as r:
        data = json.load(r)
except Exception as e:
    print("nicht erreichbar/Timeout: %s" % e); sys.exit(2)
msg = (data.get("choices") or [{}])[0].get("message") or {}
text = ((msg.get("content") or "") or (msg.get("reasoning") or "")).strip()
def why_garbage(t):
    if not t:
        return "leere Antwort"
    if re.search(r"(\S)\1{14,}", t):            # ein Zeichen 15x am Stueck ("!!!!")
        return "ein Zeichen 15x wiederholt"
    toks = t.split()
    if len(toks) >= 12 and len(set(toks)) / len(toks) < 0.2:  # "d d d d ..."
        return "nur %d verschiedene Tokens in %d" % (len(set(toks)), len(toks))
    return ""
why = why_garbage(text)
snip = text[:140].replace("\n", " ")
if why:
    print("%s :: %r" % (why, snip)); sys.exit(1)
print("%r" % snip); sys.exit(0)
PY
)"
  rc=$?
  case "$rc" in
    0) echo "   + Sanity OK -> Main-LLM antwortet kohaerent: $out" ;;
    1) echo "   !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
       echo "   !!! SANITY-FEHLER: Das Hauptmodell liefert UNSINN ($out)"
       echo "   !!! Server ist oben, aber Chat-Antworten sind MUELL. Ursache meist"
       echo "   !!! Kernel/Quant auf dieser GPU (SM120): NVFP4/Marlin bzw. DeepGEMM-E8M0."
       echo "   !!! Fix in main-qwen-plain: FP8-Modell + VLLM_USE_DEEP_GEMM=0."
       echo "   !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!" ;;
    2) echo "   ! Sanity uebersprungen: Main $out -> spaeter manuell testen." ;;
    *) echo "   ! Sanity: unerwarteter Status ($rc): $out" ;;
  esac
  return 0   # Sanity bricht den Start NIE ab (nur Diagnose)
}

if [ "$SERIAL_GPU_LOAD" = "1" ]; then
  echo ">> Serielles GPU-Laden AN (kein gestapelter Lade-Peak). Aus: SERIAL_GPU_LOAD=0 ./start.sh"
  MAIN_SVC="vllm-main"
  [ "$has_qwen_plain" = 1 ] && MAIN_SVC="vllm-main-qwen-plain"
  [ "$has_nemotron" = 1 ] && MAIN_SVC="vllm-main-nemotron"
  echo ">> [seriell 1/4] Hauptmodell ($MAIN_SVC)"
  retry 3 dc up -d --no-deps "$MAIN_SVC";  wait_health "http://localhost:5568/health" "vLLM main" 30
  echo ">> [seriell 2/4] Mem0-Embedder (TEI bge-m3)"
  retry 3 dc up -d --no-deps embeddings;   wait_health "http://localhost:8082/health" "TEI bge-m3" 15
  echo ">> [seriell 3/4] RAGFlow/Morphik-Embedder (vllm-embed)"
  retry 3 dc up -d --no-deps vllm-embed;   wait_health "http://localhost:8091/health" "vLLM embed" 20
  case "$SEEN" in
    *" helper "*)
      echo ">> [seriell 4/4] Helfer (vllm-helper)"
      retry 3 dc --profile helper up -d --no-deps vllm-helper
      wait_health "http://localhost:30001/health" "vLLM helper" 15 ;;
    *) echo "   (Helfer aus -> uebersprungen)";;
  esac
  echo ">> GPU-Modelle geladen -> jetzt den Rest (morphik/ColPali laedt nun allein)"
fi

if ! retry 3 main_up; then
  echo "   !! Voller Start fehlgeschlagen (meist EIN kaputter Build/Pull)."
  echo "   -> Rettungsversuch: Kernstack OHNE optionale Profile starten,"
  echo "      damit der Stack trotzdem nutzbar ist."
  docker compose "${COMPOSE_FILES[@]}" "${MAIN_PROFILE_ARGS[@]}" up -d 2>&1 || true
  echo "   -> Danach die Ursache oben suchen (Stichwort 'failed to solve'/'not found')"
  echo "      und das betroffene Profil gezielt nachziehen."
fi

# --- Sanity-Check Hauptmodell: erst auf Bereitschaft warten, dann 1 Testanfrage ---
#  Deckt beide Modi ab: seriell (Main laengst oben -> wait_health kehrt sofort zurueck)
#  und parallel (main_up hat gerade gestartet -> hier warten wir das Laden/Warmup ab).
if [ "$MAIN_SANITY_CHECK" = "1" ]; then
  echo ">> Sanity-Check Hauptmodell (wartet auf Bereitschaft, dann 1 Testanfrage)"
  wait_health "http://localhost:5568/health" "vLLM main" 30
  main_sanity_check
fi

# --- Abschluss-Check: erreichen wir JEDEN erwarteten Dienst? -----------------
#  Health=200 pro Dienst. Profil-abhaengige Dienste werden nur geprueft, wenn ihr
#  Profil aktiv ist. Ergebnis als kompakte OK/FEHLT-Liste (bricht nie ab).
check_all() {
  local ok=0 bad=0 line name url
  # name|url|bedingung ("" = immer, sonst Profilname)
  local CHECKS=(
    "vLLM main (5568)|http://localhost:5568/health|"
    "vLLM embed (8091)|http://localhost:8091/health|"
    "TEI bge-m3 (8082)|http://localhost:8082/health|"
    "Qdrant (6333)|http://localhost:6333/readyz|"
    "research-agent (9009)|http://localhost:9009/healthz|"
    "ingest-router (9010)|http://localhost:9010/healthz|"
    "Open WebUI (3009)|http://localhost:3009/health|"
    "RAGFlow UI (80)|http://localhost/|"
    "Grafana (${GRAFANA_PORT:-3011})|http://localhost:${GRAFANA_PORT:-3011}/api/health|OBS"
    "Prometheus (${PROMETHEUS_PORT:-9090})|http://localhost:${PROMETHEUS_PORT:-9090}/-/healthy|OBS"
    "Dozzle (${DOZZLE_PORT:-8085})|http://localhost:${DOZZLE_PORT:-8085}/|OBS"
    "vLLM helper (30001)|http://localhost:30001/health|helper"
    "Morphik (8083)|http://localhost:8083/health|morphik"
    "microVM-Executor (8077)|http://localhost:8077/healthz|microvm"
    "computer-use (8084)|http://localhost:8084/|computer-use"
    "Fragments (3010)|http://localhost:3010/|fragments"
  )
  echo "   --- Erreichbarkeit (HTTP) ---"
  for line in "${CHECKS[@]}"; do
    name="${line%%|*}"; url="${line#*|}"; local cond="${url#*|}"; url="${url%%|*}"
    case "$cond" in
      "") : ;;                                     # immer pruefen
      SKIP) continue ;;                            # nur intern erreichbar
      OBS) [ "$LOGGING_STACK" = "1" ] || continue ;;
      *) case "$SEEN" in *" $cond "*) : ;; *) continue ;; esac ;;
    esac
    if http_ok "$url"; then
      printf "     [ OK   ] %s\n" "$name"; ok=$((ok+1))
    else
      printf "     [ FEHLT] %s  -> %s\n" "$name" "$url"; bad=$((bad+1))
    fi
  done
  echo "   --- $ok erreichbar, $bad nicht erreichbar ---"
  if [ "$bad" -gt 0 ]; then
    echo "   ! Nicht erreichbare Dienste koennen noch STARTEN (grosse Modelle/Images)."
    echo "     Pruefen:  docker compose ps   |   docker logs <container>   |   Dozzle/Grafana"
  fi
}

# SOLL-/IST-Vergleich: welche Dienste der aktiven Profile laufen NICHT?
#  Wichtig: nicht nur die von 'compose ps' gelisteten pruefen — ein Dienst, der nie
#  erstellt wurde (Build/Pull-Fehler, Namenskonflikt), taucht dort GAR NICHT auf und
#  waere sonst unsichtbar ("alle gestarteten laufen" trotz fehlender Dienste).
check_containers() {
  echo "   --- Dienste (Soll/Ist) ---"
  local want have missing bad
  want="$(dc config --services 2>/dev/null | sort -u)"
  have="$(dc ps --services --status running 2>/dev/null | sort -u)"
  if [ -z "$want" ]; then echo "     (Dienstliste nicht ermittelbar)"; return 0; fi
  missing="$(comm -23 <(printf '%s\n' "$want") <(printf '%s\n' "$have") 2>/dev/null)"
  bad="$(dc ps -a --format '{{.Service}}\t{{.State}}\t{{.Status}}' 2>/dev/null \
         | grep -viE '\brunning\b' || true)"
  echo "     erwartet: $(printf '%s\n' "$want" | grep -c .)   laufend: $(printf '%s\n' "$have" | grep -c .)"
  if [ -n "$missing" ]; then
    echo "   ! Diese Dienste laufen NICHT:"
    printf '%s\n' "$missing" | sed 's/^/       - /'
    [ -n "$bad" ] && { echo "     Details (Status):"; printf '%s\n' "$bad" | sed 's/^/       /'; }
    echo "     -> Ursache:  docker compose logs --tail=40 <dienst>"
  else
    echo "     [ OK ] alle erwarteten Dienste laufen."
  fi
}

# Dienste OHNE Host-Port (nur im Docker-Netz erreichbar) -> ueber Container-Status
# und den vom Image mitgebrachten Healthcheck pruefen, nicht ueber localhost.
check_internal() {
  echo "   --- Interne Dienste (kein Host-Port) ---"
  local line name cn cond st hs
  local ICHECKS=(
    "mem0-struct|mem0_struct|mem0struct"
    "SearXNG|searxng|"
    "presidio-proxy|presidio_proxy|"
    "code-sandbox|code_sandbox|"
    "browserless|browserless|"
    "Loki|loki|OBS"
    "Promtail|promtail|OBS"
  )
  for line in "${ICHECKS[@]}"; do
    name="${line%%|*}"; cn="${line#*|}"; cond="${cn#*|}"; cn="${cn%%|*}"
    case "$cond" in
      "") : ;;
      OBS) [ "$LOGGING_STACK" = "1" ] || continue ;;
      *) case "$SEEN" in *" $cond "*) : ;; *) continue ;; esac ;;
    esac
    if ! docker inspect "$cn" >/dev/null 2>&1; then
      printf "     [ FEHLT] %-16s (kein Container)\n" "$name"; continue
    fi
    st="$(docker inspect -f '{{.State.Status}}' "$cn" 2>/dev/null)"
    hs="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{end}}' "$cn" 2>/dev/null)"
    if [ "$st" = "running" ] && { [ -z "$hs" ] || [ "$hs" = "healthy" ]; }; then
      printf "     [ OK   ] %-16s (%s%s)\n" "$name" "$st" "${hs:+/$hs}"
    else
      printf "     [ PRUEF] %-16s (%s%s) -> docker logs --tail=40 %s\n" "$name" "$st" "${hs:+/$hs}" "$cn"
    fi
  done

  # Datei-Browser der rechten OWUI-Seitenleiste: die Sandbox liefert dafuer eine
  # Datei-API und meldet features.terminal=false (Browser ja, Shell nein).
  if docker inspect code_sandbox >/dev/null 2>&1; then
    local fb
    fb="$(docker exec code_sandbox python3 -c "
import json, os, urllib.request
t = os.environ.get('SANDBOX_FILES_TOKEN', '')
if not t:
    print('AUS')
else:
    req = urllib.request.Request('http://127.0.0.1:8000/api/config',
                                 headers={'Authorization': 'Bearer ' + t})
    d = json.load(urllib.request.urlopen(req, timeout=5))
    print('OK' if d.get('features', {}).get('terminal') is False else 'UNERWARTET')
" 2>/dev/null)"
    case "$fb" in
      OK)  printf "     [ OK   ] %-16s (Dateien je Chat in OWUIs rechter Seitenleiste)\n" "Datei-Browser" ;;
      AUS) printf "     [ AUS  ] %-16s (SANDBOX_FILES_TOKEN fehlt -> siehe docs/OWUI_DATEIBROWSER.md)\n" "Datei-Browser" ;;
      *)   printf "     [ PRUEF] %-16s -> docker logs --tail=40 code_sandbox\n" "Datei-Browser" ;;
    esac
  fi
}

echo "================================================================"
echo ">> Abschluss-Pruefung"
check_containers
check_all
check_internal

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
