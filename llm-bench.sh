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
#    ./llm-bench.sh -v                  # VERGLEICH 1 vs. 4: skaliert der Stack?
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
VERGLEICH=0
PROMPT="Erklaere in genau zehn nummerierten Punkten, wie ein Sozialversicherungstraeger \
monatliche Bestandsdateien auf Auffaelligkeiten pruefen sollte. Antworte sachlich und knapp."

while [ $# -gt 0 ]; do
  case "$1" in
    -p|--parallel) PAR="$2"; shift 2 ;;
    -v|--vergleich) VERGLEICH=1; shift ;;
    -t|--tokens)   TOK="$2"; shift 2 ;;
    --url)         BASE="$2"; shift 2 ;;
    --model)       MODEL="$2"; shift 2 ;;
    --prompt)      PROMPT="$2"; shift 2 ;;
    -h|--help)     sed -n '2,22p' "$0"; exit 0 ;;
    *) echo "Unbekannte Option: $1"; exit 1 ;;
  esac
done

command -v python3 >/dev/null 2>&1 || { echo "python3 wird gebraucht."; exit 1; }

BASE="$BASE" MODEL="$MODEL" PAR="$PAR" TOK="$TOK" PROMPT="$PROMPT" VERGLEICH="$VERGLEICH" python3 - <<'PY'
import json, os, sys, threading, time, urllib.error, urllib.request
from concurrent.futures import ThreadPoolExecutor

BASE = os.environ["BASE"].rstrip("/")
MODEL, PROMPT = os.environ["MODEL"], os.environ["PROMPT"]
PAR, TOK = int(os.environ["PAR"]), int(os.environ["TOK"])
VERGLEICH = os.environ.get("VERGLEICH") == "1"
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
                # Je nach Reasoning-Parser heisst das Feld 'content',
                # 'reasoning_content' ODER 'reasoning' -> generisch pruefen.
                if any(isinstance(v, str) and v for k, v in delta.items() if k != "role"):
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


def messlauf(par):
    """Einmal messen: par parallele Anfragen -> Kennzahlen."""
    vor = scrape()
    probe = {"kv": 0.0, "laufend": 0.0, "wartend": 0.0}
    stop = threading.Event()

    def sampler():
        # Gauges sind Momentwerte — nach dem Lauf stehen sie auf 0.
        # Also waehrend des Laufs abtasten und die Spitzen behalten.
        while not stop.is_set():
            m = scrape()
            probe["kv"] = max(probe["kv"], m.get("vllm:kv_cache_usage_perc", 0.0))
            probe["laufend"] = max(probe["laufend"], m.get("vllm:num_requests_running", 0.0))
            probe["wartend"] = max(probe["wartend"], m.get("vllm:num_requests_waiting", 0.0))
            stop.wait(0.25)

    th = threading.Thread(target=sampler, daemon=True)
    th.start()
    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=par) as ex:
        res = list(ex.map(one, range(par)))
    wand = time.perf_counter() - t0
    stop.set()
    th.join(timeout=2)
    nach = scrape()

    fehler = [r["fehler"] for r in res if "fehler" in r]
    res = [r for r in res if "fehler" not in r]
    if not res:
        return {"fehler": fehler[0] if fehler else "keine Antwort"}

    def d(k):
        return nach.get(k, 0.0) - vor.get(k, 0.0)

    aus_ges = sum(r["aus"] for r in res)
    je = [r["aus"] / r["dauer"] for r in res if r["dauer"] > 0]
    ttfts = sorted(r["ttft"] for r in res if r["ttft"] is not None)
    return {
        "par": par, "wand": wand, "aus": aus_ges,
        "ein": sum(r["ein"] or 0 for r in res),
        "ges_tps": aus_ges / wand if wand else 0.0,
        "je_tps": sum(je) / len(je) if je else 0.0,
        "ttft_med": ttfts[len(ttfts) // 2] if ttfts else None,
        "ttft_max": ttfts[-1] if ttfts else None,
        "akz": d("vllm:spec_decode_num_accepted_tokens_total"),
        "entw": d("vllm:spec_decode_num_draft_tokens_total"),
        "drafts": d("vllm:spec_decode_num_drafts_total"),
        "verdr": d("vllm:num_preemptions_total"),
        "pc_h": d("vllm:prefix_cache_hits_total"),
        "pc_q": d("vllm:prefix_cache_queries_total"),
        "kv": probe["kv"] * 100, "laufend": probe["laufend"], "wartend": probe["wartend"],
        "fehler_n": len(fehler), "fehler1": fehler[0] if fehler else None,
    }


def ausgabe(m):
    print(f"""
  ── Ergebnis ({m['par']} parallel) ──────────────────────────────────
  Dauer gesamt          {hr(m['wand'], 2)} s
  Erzeugte Token        {m['aus']}   (Prompt: {m['ein']})

  Durchsatz GESAMT      {hr(m['ges_tps'])} Token/s      <- zaehlt bei mehreren Nutzern
  Tempo je Anfrage      {hr(m['je_tps'])} Token/s      <- so fuehlt es sich an
  Zeit je Token         {hr(1000 / m['je_tps'] if m['je_tps'] else None, 1)} ms

  Wartezeit 1. Token    Mitte {hr(m['ttft_med'], 2)} s""" +
          (f"   langsamste {hr(m['ttft_max'], 2)} s" if m["par"] > 1 else ""))
    if m["entw"] > 0:
        print(f"  Spekulation           {100 * m['akz'] / m['entw']:.0f} % angenommen"
              + (f"   ({m['akz'] / m['drafts']:.1f} von {SPEC_SOLL} Token je Entwurf)"
                 if m["drafts"] > 0 else ""))
    else:
        print("  Spekulation           nicht aktiv")
    if m["pc_q"] > 0:
        print(f"  Prompt aus dem Cache  {100 * m['pc_h'] / m['pc_q']:.0f} %")
    print(f"  KV-Cache (Spitze)     {m['kv']:.0f} %")
    print(f"  Gleichzeitig aktiv    {m['laufend']:.0f}"
          + (f"   wartend: {m['wartend']:.0f}" if m["wartend"] else ""))
    print(f"  Verdraengungen        {m['verdr']:.0f}"
          + ("   << KV-Cache zu klein!" if m["verdr"] > 0 else ""))
    if m["fehler_n"]:
        print(f"  Fehlgeschlagen        {m['fehler_n']} Anfrage(n): {str(m['fehler1'])[:120]}")


SPEC_SOLL = os.environ.get("NEMOTRON_SPEC_TOKENS", "3")

print("  Aufwaermen ...", flush=True)
one(0)                                   # Kernel/Graphs warm, Prefix-Cache gefuellt

laeufe = []
for par in ([1, PAR if PAR > 1 else 4] if VERGLEICH else [PAR]):
    m = messlauf(par)
    if "fehler" in m:
        print("\n  FEHLGESCHLAGEN:", m["fehler"])
        sys.exit(1)
    laeufe.append(m)
    ausgabe(m)

tips = []
if len(laeufe) == 2:
    a, b = laeufe
    faktor = b["ges_tps"] / a["ges_tps"] if a["ges_tps"] else 0.0
    print(f"""
  ── Skalierung {a['par']} -> {b['par']} parallel ───────────────────────────────
  Durchsatz GESAMT      {hr(a['ges_tps'])}  ->  {hr(b['ges_tps'])} Token/s   (Faktor {faktor:.2f})
  Tempo je Anfrage      {hr(a['je_tps'])}  ->  {hr(b['je_tps'])} Token/s""")
    if faktor >= 1.6:
        tips.append(f"Der Durchsatz skaliert ({faktor:.1f}x) — die GPU hatte Luft.\n"
                    f"    NEMOTRON_MAX_SEQS weiter erhoehen bringt noch mehr.")
    elif faktor >= 1.2:
        tips.append(f"Maessige Skalierung ({faktor:.1f}x) — der Punkt der Saettigung ist nah.\n"
                    f"    Mehr Parallelitaet kostet ab hier vor allem Latenz.")
    else:
        tips.append(f"KEINE Skalierung ({faktor:.2f}x): die GPU ist schon bei einer Anfrage\n"
                    f"    ausgelastet. Mehr Parallelitaet bringt keinen Durchsatz mehr, sondern\n"
                    f"    verteilt ihn nur — je Anfrage wird es langsamer. NEMOTRON_MAX_SEQS\n"
                    f"    dient dann nur noch dazu, dass Sub-Agents nicht in der Warteschlange\n"
                    f"    stehen. Der verbleibende Hebel ist die spekulative Dekodierung.")

m = laeufe[-1]
print("""
  ── Einordnung ──────────────────────────────────────────────────""")
if len(laeufe) == 1 and PAR == 1:
    tips.append("Mit -v gegenpruefen: misst 1 und 4 parallel und sagt, ob der Stack skaliert.")
if m["verdr"] > 0:
    tips.append("Verdraengungen > 0: der KV-Cache reicht nicht. VRAM freimachen (Profile\n"
                "    'helper'/'morphik' aus) oder NEMOTRON_MAX_SEQS wieder senken.")
if m["entw"] > 0 and m["akz"] / m["entw"] > 0.55:
    tips.append(f"Akzeptanzrate {100 * m['akz'] / m['entw']:.0f} %: NEMOTRON_SPEC_TOKENS von "
                f"{SPEC_SOLL} auf {int(SPEC_SOLL) + 1} erhoehen und erneut messen —\n"
                "    das ist bei gesaettigter GPU der wirksamste verbleibende Hebel.")
if m["entw"] > 0 and m["akz"] / m["entw"] < 0.3:
    tips.append("Akzeptanzrate unter 30 %: das Raten kostet mehr als es bringt —\n"
                "    NEMOTRON_SPEC_TOKENS senken.")
if m["kv"] > 85:
    tips.append("KV-Cache ueber 85 %: bei laengeren Chats drohen Verdraengungen.")
if m["wartend"] > 0:
    tips.append("Anfragen mussten warten -> NEMOTRON_MAX_SEQS ist zu klein fuer diese Last.")
if not tips:
    tips.append("Werte unauffaellig. Verlauf im Dashboard: Grafana -> 'AI-Stack — LLM-Leistung'.")
for t in tips:
    print("  *", t)
print()
PY
