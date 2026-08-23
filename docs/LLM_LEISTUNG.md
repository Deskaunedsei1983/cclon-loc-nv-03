# LLM-Leistung: messen, verstehen, verbessern

## Warum die Wattzahl nichts sagt

Aus `nvidia-smi dmon` während einer laufenden Generierung:

```
gpu    pwr  gtemp     sm    mem   mclk   pclk     fb
  0    284     46     79     41  13365   2820  93702
```

Das liest sich als „nur 284 von 600 W" — tatsächlich steht dort:

* **`sm 79 %`** — die Rechenwerke sind gut ausgelastet, von Leerlauf keine Spur.
* **`mem 41 %`** — der Speicher-Controller ist der ruhigere Teil.
* **`pclk 2820 MHz`, `gtemp 46 °C`** — voller Boost, keine Drosselung.
* **`fb 93702 MB`** — **93,7 GB von 96 GB belegt.** Das ist die eigentliche Grenze.

Beim Erzeugen von Token (Decode) liest die GPU pro Token einmal die Gewichte und
rechnet vergleichsweise wenig damit. Das ist **speicherlimitiert**, nicht
rechenlimitiert — niedrige Wattzahl ist dabei der Normalfall, kein Defekt. Dazu
kommt: Nemotron 3.5 Lightning ist ein **A3B**-Modell, von 30 Mrd. Parametern sind
pro Token nur ~3 Mrd. aktiv (MoE), und die liegen als NVFP4 mit 4 Bit vor. Es
wird also wenig gerechnet *und* wenig gelesen.

Volle Leistung sieht man im **Prefill** (Prompt einlesen) — dort spitzt die
Kurve kurz, danach fällt sie wieder.

**Aussagekräftig sind Token/s, Wartezeit und Verdrängungen.** Dafür gibt es jetzt
zwei Werkzeuge.

## 1. `./llm-bench.sh` — die schnelle Zahl

Fährt eine definierte Last direkt gegen vLLM (nicht gegen den Agenten — sonst
misst man RAG, Websuche und Sandbox mit) und schreibt Klartext:

```bash
./llm-bench.sh          # 1 Anfrage  -> wie es sich anfühlt (Latenz)
./llm-bench.sh -p 4     # 4 parallel -> wie der Stack skaliert (Durchsatz)
./llm-bench.sh -p 8 -t 512
```

```
  Durchsatz GESAMT      1128.2 Token/s      <- zählt bei mehreren Nutzern
  Tempo je Anfrage       286.4 Token/s      <- so fühlt es sich an
  Zeit je Token            3.5 ms
  Wartezeit 1. Token    Mitte 0.15 s   langsamste 0.15 s
  Spekulation           70 % angenommen   (2.1 Token je Entwurf)
  KV-Cache              42 %
  Verdrängungen         0
```

Darunter steht eine **Einordnung**, die aus den gemessenen Werten die nächste
Maßnahme nennt. Ein Aufwärmlauf vorab sorgt dafür, dass CUDA-Graphs und
Prefix-Cache warm sind — sonst misst man den ersten Start mit.

Der entscheidende Vergleich ist `-p 1` gegen `-p 4`: **steigt der
Gesamt-Durchsatz deutlich**, war die GPU vorher unterfordert.

## 2. Grafana-Dashboard „AI-Stack — LLM-Leistung"

`http://localhost:3011` → Dashboard **AI-Stack — LLM-Leistung (Token, Wartezeit, GPU)**

Prometheus scrapt vLLM jetzt alle 5 Sekunden (`/metrics` liegt auf demselben Port
wie die API). Das Dashboard beginnt mit einer Erklärtafel und zeigt dann:

| Bereich | Panels |
|---|---|
| Auf einen Blick | Antwort-Token/s, Prompt-Token/s, Wartezeit bis 1. Token (p95), Zeit pro Token (p95), KV-Cache, laufende/wartende Anfragen, Verdrängungen, Prefix-Cache-Treffer, GPU-Auslastung |
| Zeitverlauf | Token/s (Ausgabe vs. Eingabe), Anfragen laufend/wartend, TTFT p50/p95, Zeit pro Token p50/p95 |
| Spekulative Dekodierung | Akzeptanzrate, Token je Entwurf, plus Erklärung was damit zu tun ist |
| GPU | Rechenwerke vs. Speicher-Controller, Leistungsaufnahme, VRAM, KV-Cache-Verlauf |
| Was tun, wenn … | Entscheidungstabelle Beobachtung → Ursache → Maßnahme |

Die verwendeten Metriknamen stammen aus dem vLLM-Quelltext **v0.27.1**
(`vllm/v1/metrics/loggers.py`, `vllm/v1/spec_decode/metrics.py`) — also exakt der
Version, die `docker-compose.yml` pinnt. Nach einem vLLM-Upgrade gehört das
gegengeprüft:

```bash
curl -s localhost:5568/metrics | grep -oE '^vllm:[a-z_]+' | sort -u
```

## 3. Die Maßnahmen

### Mehr Parallelität — der wirksamste Hebel

`--max-num-seqs` stand auf **2**. Bei einer einzelnen laufenden Anfrage arbeitet
die GPU im ineffizientesten Punkt überhaupt: die Gewichte werden pro Token einmal
gelesen, **egal für wie viele Anfragen**. Zwei parallele Anfragen teilen sich
diesen Lesevorgang, vier ebenso — der Gesamtdurchsatz steigt fast linear, während
die Latenz je Anfrage kaum leidet.

Neu: `NEMOTRON_MAX_SEQS` (Default **4** = eine Hauptanfrage + die drei
Sub-Agents aus `SUBAGENTS_MAX_CONCURRENT`), analog `VLLM_MAX_SEQS` für die
Qwen-Profile.

> **Vorsicht bei Nemotron:** das Modell ist ein Mamba-Hybrid, der SSM-Zustand wird
> **pro Sequenz-Slot** vorgehalten. Höhere Werte kosten also VRAM — und davon ist
> bei euch fast nichts frei. Nach dem Erhöhen prüfen, ob der Container startet,
> und im Dashboard auf *Verdrängungen* schauen.

### VRAM ist der Engpass, nicht die Rechenleistung

93,7 von 96 GB sind belegt. Was hier fehlt, fehlt dem KV-Cache — und damit der
Parallelität. Der größte Hebel ist deshalb **Aufräumen**:

```bash
# Was belegt gerade wie viel?
nvidia-smi --query-compute-apps=pid,used_memory,process_name --format=csv
```

Kandidaten zum Abschalten, wenn nicht gebraucht (in `COMPOSE_PROFILES`):

| Profil | grob | wofür |
|---|---|---|
| `helper` | ~8 GB | kleines Modell für Titel/Tags/Suchqueries — `mem0-struct` (CPU) kann das auch |
| `morphik` | ~14 GB | multimodales RAG; lädt ColPali **zweimal** (ARQ-Worker + uvicorn) |
| `main-qwen*` | — | darf ohnehin nie parallel zu `main-nemotron` laufen |

Erst danach lohnt es, `NEMOTRON_GPU_UTIL` zu erhöhen — mehr Anteil bedeutet
größerer KV-Cache und damit mehr gleichzeitige Gespräche.

### Spekulative Dekodierung nachziehen

`NEMOTRON_SPEC_TOKENS` steht auf 3. Ob das passt, sagt die **Akzeptanzrate**
(Dashboard und `llm-bench.sh`):

* **> 60 %** — das Entwurfsmodell trifft gut, auf 4–5 erhöhen.
* **< 30 %** — das Raten kostet mehr als es bringt, auf 2 senken.

### Prefill-Bündel

`NEMOTRON_MAX_BATCHED_TOKENS` (Default 16384) bestimmt, wie viele Token pro
Engine-Schritt eingelesen werden. Größer = schnellerer Prefill bei langen
Dokumenten, kostet aber Aktivierungsspeicher. Nur anfassen, wenn die Wartezeit
bis zum ersten Token stört **und** VRAM frei ist.

### Was NICHT hilft

* Auf die Wattzahl schauen. Mehr Watt ist kein Ziel; Token/s ist eins.
* `--enforce-eager` — steht bei uns bewusst nicht drin (schaltet CUDA-Graphs ab
  und macht Decode deutlich langsamer). War nur ein Debug-Hebel.
* Größeres `--gpu-memory-utilization` bei vollem VRAM: der Container startet
  dann gar nicht erst.

## Vorgehen in vier Schritten

```bash
# 1. Ausgangslage messen
./llm-bench.sh > /tmp/vorher.txt
./llm-bench.sh -p 4 >> /tmp/vorher.txt

# 2. Eine Sache ändern (z.B. in der .env)
#    NEMOTRON_MAX_SEQS=6
docker compose up -d vllm-main-nemotron

# 3. Nachmessen
./llm-bench.sh -p 4

# 4. Im Dashboard gegenprüfen: Verdrängungen weiterhin 0? KV-Cache < 85 %?
```

Immer nur **eine** Stellschraube pro Durchgang — sonst weiß man am Ende nicht,
was gewirkt hat.
