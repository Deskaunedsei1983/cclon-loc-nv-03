#!/usr/bin/env bash
# ============================================================================
#  llm-bench.sh — Wie schnell ist das Hauptmodell WIRKLICH?
# ============================================================================
#  Faehrt eine definierte Last gegen vLLM und gibt die Zahlen in Klartext aus:
#  Token/s gesamt und je Anfrage, Wartezeit bis zum ersten Token, Tempo pro
#  Token, Akzeptanzrate der spekulativen Dekodierung, KV-Cache und
#  Verdraengungen. Vorher/nachher vergleichbar — damit man Aenderungen an
#  NEMOTRON_MAX_SEQS oder NEMOTRON_SPEC_TOKENS misst statt raet.
#
#  Aufruf:
#    ./llm-bench.sh                     # 1 Anfrage  (Latenz, wie es sich anfuehlt)
#    ./llm-bench.sh -p 4                # 4 parallel (Durchsatz, wie der Stack skaliert)
#    ./llm-bench.sh -p 8 -t 512         # 8 parallel, je 512 Token
#    ./llm-bench.sh --url http://localhost:30001/v1 --model qwen-helper
#
#  Braucht nur python3 (Standardbibliothek). Laeuft NICHT gegen den Agenten,
#  sondern direkt gegen vLLM — sonst misst man RAG, Websuche und Sandbox mit.
# ============================================================================
set -euo pipefail
cd "$(dirname "$0")"

BASE="${LLM_BENCH_URL:-http://localhost:5568/v1}"
MODEL="${LLM_BENCH_MODEL:-main}"
PAR=1
TOK=256
PROMPT="Erklaere in genau zehn nummerierten Punkten, wie ein Sozialversicherungstraeger \
monatliche Bestandsdateien auf Auffaelligkeiten pruefen sollte. Antworte sachlich und knapp."

while [ $# -gt 0 ]; do
  case "$1" in
    -p|--parallel) PAR="$2"; shift 2 ;;
    -t|--tokens)   TOK="$2"; shift 2 ;;
    --url)         BASE="$2"; shift 2 ;;
    --model)       MODEL="$2"; shift 2 ;;
    --prompt)      PROMPT="$2"; shift 2 ;;
    -h|--help)     sed -n '2,22p' "$0"; exit 0 ;;
    *) echo "Unbekannte Option: $1"; exit 1 ;;
  esac
done

command -v python3 >/dev/null 2>&1 || { echo "python3 wird gebraucht."; exit 1; }

BASE="$BASE" MODEL="$MODEL" PAR="$PAR" TOK="$TOK" PROMPT="$PROMPT" python3 - <<'PY'
import json, os, sys, time, urllib.error, urllib.request
from concurrent.futures import ThreadPoolExecutor

BASE = os.environ["BASE"].rstrip("/")
MODEL, PROMPT = os.environ["MODEL"], os.environ["PROMPT"]
PAR, TOK = int(os.environ["PAR"]), int(os.environ["TOK"])
METRICS = BASE[:-3].rstrip("/") + "/metrics" if BASE.endswith("/v1") else BASE + "/metrics"


def scrape():
    """Prometheus-Text von vLLM -> {name: summe ueber alle Labels}."""
    out = {}
    try:
        with urllib.request.urlopen(METRICS, timeout=5) as r:
            for line in r.read().decode("utf-8", "replace").splitlines():
                if not line or line[0] == "#":
                    continue
                name, _, rest = line.partition("{")
                if rest:
                    name_full = name
                    val = rest.rsplit("}", 1)[-1]
                else:
                    name_full, _, val = line.partition(" ")
                try:
                    out[name_full] = out.get(name_full, 0.0) + float(val)
                except ValueError:
                    pass
    except Exception:
        pass
    return out


def one(i):
    """Eine gestreamte Anfrage; misst Wartezeit bis zum ersten Token."""
    body = json.dumps({
        "model": MODEL,
        "messages": [{"role": "user", "content": PROMPT}],
        "max_tokens": TOK, "temperature": 0.7, "stream": True,
        "stream_options": {"include_usage": True},
    }).encode()
    req = urllib.request.Request(BASE + "/chat/completions", data=body,
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer not-needed"})
    t0 = time.perf_counter()
    ttft = None
    n = 0
    usage = None
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            for raw in r:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    d = json.loads(payload)
                except ValueError:
                    continue
                if d.get("usage"):
                    usage = d["usage"]
                ch = (d.get("choices") or [{}])[0]
                delta = ch.get("delta") or {}
                if delta.get("content") or delta.get("reasoning_content"):
                    if ttft is None:
                        ttft = time.perf_counter() - t0
                    n += 1
    except urllib.error.HTTPError as e:
        return {"fehler": f"HTTP {e.code}: {e.read()[:200].decode('utf-8', 'replace')}"}
    except Exception as e:
        return {"fehler": f"{type(e).__name__}: {e}"}
    dauer = time.perf_counter() - t0
    aus = (usage or {}).get("completion_tokens") or n
    ein = (usage or {}).get("prompt_tokens")
    return {"ttft": ttft, "dauer": dauer, "aus": aus, "ein": ein, "chunks": n}


def hr(x, nk=1):
    return "—" if x is None else f"{x:.{nk}f}"


print(f"\n  Ziel      : {BASE}   Modell: {MODEL}")
print(f"  Last      : {PAR} parallele Anfrage(n), je bis zu {TOK} Token")
print("  Aufwaermen ...", flush=True)
one(0)                                   # Kernel/Graphs warm, Prefix-Cache gefuellt

vor = scrape()
t0 = time.perf_counter()
with ThreadPoolExecutor(max_workers=PAR) as ex:
    res = list(ex.map(one, range(PAR)))
wand = time.perf_counter() - t0
nach = scrape()

fehler = [r["fehler"] for r in res if "fehler" in r]
res = [r for r in res if "fehler" not in r]
if not res:
    print("\n  FEHLGESCHLAGEN:", fehler[0] if fehler else "keine Antwort")
    sys.exit(1)

aus_ges = sum(r["aus"] for r in res)
ein_ges = sum(r["ein"] or 0 for r in res)
ttfts = sorted(r["ttft"] for r in res if r["ttft"] is not None)
je_anfrage = [r["aus"] / r["dauer"] for r in res if r["dauer"] > 0]


def d(k):
    return nach.get(k, 0.0) - vor.get(k, 0.0)


akz, entw, drafts = d("vllm:spec_decode_num_accepted_tokens_total"), \
    d("vllm:spec_decode_num_draft_tokens_total"), d("vllm:spec_decode_num_drafts_total")
verdr = d("vllm:num_preemptions_total")
kv = nach.get("vllm:kv_cache_usage_perc", 0.0) * 100
pc_hits, pc_q = d("vllm:prefix_cache_hits_total"), d("vllm:prefix_cache_queries_total")

print(f"""
  ── Ergebnis ────────────────────────────────────────────────────
  Dauer gesamt          {hr(wand, 2)} s
  Erzeugte Token        {aus_ges}   (Prompt: {ein_ges})

  Durchsatz GESAMT      {hr(aus_ges / wand)} Token/s      <- zaehlt bei mehreren Nutzern
  Tempo je Anfrage      {hr(sum(je_anfrage) / len(je_anfrage))} Token/s      <- so fuehlt es sich an
  Zeit je Token         {hr(1000 * len(je_anfrage) / max(sum(je_anfrage), 1e-9), 1)} ms

  Wartezeit 1. Token    Mitte {hr(ttfts[len(ttfts) // 2], 2) if ttfts else '—'} s""" +
      (f"   langsamste {hr(ttfts[-1], 2)} s" if len(ttfts) > 1 else ""))

if entw > 0:
    print(f"  Spekulation           {100 * akz / entw:.0f} % angenommen"
          + (f"   ({akz / drafts:.1f} Token je Entwurf)" if drafts > 0 else ""))
else:
    print("  Spekulation           nicht aktiv")
if pc_q > 0:
    print(f"  Prompt aus dem Cache  {100 * pc_hits / pc_q:.0f} %")
print(f"  KV-Cache              {kv:.0f} %")
print(f"  Verdraengungen        {verdr:.0f}" + ("   << KV-Cache zu klein!" if verdr > 0 else ""))
if fehler:
    print(f"  Fehlgeschlagen        {len(fehler)} Anfrage(n): {fehler[0][:120]}")

print("""
  ── Einordnung ──────────────────────────────────────────────────""")
tips = []
if PAR == 1:
    tips.append("Mit -p 4 gegenpruefen: steigt der GESAMT-Durchsatz deutlich, war die\n"
                "    GPU vorher unterfordert — dann ist NEMOTRON_MAX_SEQS der richtige Hebel.")
if verdr > 0:
    tips.append("Verdraengungen > 0: der KV-Cache reicht nicht. VRAM freimachen (Profile\n"
                "    'helper'/'morphik' aus) oder NEMOTRON_MAX_SEQS wieder senken.")
if entw > 0 and akz / entw > 0.6:
    tips.append("Akzeptanzrate ueber 60 %: NEMOTRON_SPEC_TOKENS von 3 auf 4-5 erhoehen.")
if entw > 0 and akz / entw < 0.3:
    tips.append("Akzeptanzrate unter 30 %: das Raten kostet mehr als es bringt —\n"
                "    NEMOTRON_SPEC_TOKENS auf 2 senken.")
if kv > 85:
    tips.append("KV-Cache ueber 85 %: bei laengeren Chats drohen Verdraengungen.")
if not tips:
    tips.append("Werte unauffaellig. Verlauf im Dashboard: Grafana -> 'AI-Stack — LLM-Leistung'.")
for t in tips:
    print("  *", t)
print()
PY
