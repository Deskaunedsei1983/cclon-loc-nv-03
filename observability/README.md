# Zentrales Logging / Observability

Eine Sammelstelle für die Logs **aller** Container, damit nie wieder unklar ist,
„in welchem Container man schauen muss", wenn etwas klemmt (z. B. die Websuche
0 Treffer liefert).

## Bestandteile

**Logs:**

| Dienst   | Aufgabe | Zugriff |
|----------|---------|---------|
| **promtail** | Liest via Docker-Socket (read-only) die Logs **jedes** laufenden Containers (Kernstack + RAGFlow + vLLM + Upgrades) und schickt sie an Loki. Keine Per-Container-Konfig. | – |
| **loki**     | Speichert alles durchsuchbar auf Platte (14 Tage Vorhaltung). | intern `:3100` |
| **grafana**  | Eine Web-UI: Logs (Loki) **und** Metriken (Prometheus), inkl. fertiger Dashboards. | http://localhost:3011 |
| **dozzle**   | Ultraleichter **Live**-Viewer aller Container-Logs (Echtzeit-Triage). | http://localhost:8085 |

**Metriken** (das, was Dozzle NICHT kann — GPU/VRAM, Disk-I/O, Netz in/out):

| Dienst   | Aufgabe | Zugriff |
|----------|---------|---------|
| **netdata** | All-in-one **Live**-Metriken mit eigener UI: erkennt GPU/VRAM/Util, Disk-R/W, Netz in/out **automatisch** (pro Sekunde). | http://localhost:19999 |
| **prometheus** | Metrik-Speicher (15 Tage); scrapt die drei Exporter; Datenquelle in Grafana. | http://localhost:9090 |
| **node-exporter** | Host-Metriken: CPU/RAM/Disk/Netz pro Interface. | intern `:9100` |
| **cadvisor** | Metriken **pro Container** (auch interner Container-Verkehr). | intern `:8080` |
| **nvidia-exporter** | GPU: Auslastung, VRAM, Temperatur, Power (via `nvidia-smi`). | intern `:9835` |

In Grafana liegt dafür das Dashboard **„AI-Stack — Host & GPU (Metriken)"**.
Ports/Passwörter über die `.env` (`GRAFANA_PORT`, `DOZZLE_PORT`, `NETDATA_PORT`,
`PROMETHEUS_PORT`, `GRAFANA_PASSWORD`).

> **GPU-Panels leer in Grafana?** Die `nvidia_smi_*`-Metriknamen können je nach
> Exporter-Version minimal abweichen. Echte Namen prüfen:
> `docker exec prometheus wget -qO- http://nvidia-exporter:9835/metrics | grep -i util`
> und die Panel-Query anpassen. **Netdata zeigt die GPU sowieso sofort** (eigene UI).
> Reichhaltigere Fertig-Dashboards in Grafana per Import (Dashboard-ID): **1860**
> (Node Exporter Full), **14282** (cAdvisor), **14574** (nvidia_gpu_exporter) —
> jeweils Datenquelle *Prometheus* wählen.

## Starten / Stoppen

Standardmäßig **automatisch an** über `./start.sh` (alle Logs laufen sofort zusammen).

```bash
# nur den Logging-Stack (ohne den Rest) hochziehen:
docker compose -f docker-compose.observability.yml up -d

# Logging komplett aus lassen:
LOGGING_STACK=0 ./start.sh

# stoppen erledigt ./stop.sh mit (oder gezielt):
docker compose -f docker-compose.observability.yml down
```

## „Wo schauen, wenn etwas klemmt?"

1. **Grafana** öffnen → Dashboard **„AI-Stack — Container-Logs & Fehler"**.
   - Panel **„Fehler / Warnungen pro Container"** → zeigt sofort den schuldigen Container.
   - Panel **„🔴 Fehler & Warnungen — ganzer Stack"** → die Klartext-Zeilen dazu.
   - Panel **„Websuche-Pfad"** → `open-webui` · `presidio_proxy` · `searxng` gebündelt.
     Hier steht jetzt die **SearXNG-Treffer-Anzahl** (`n=0` ⇒ Engines leer/rate-limitiert
     ⇒ OWUI meldet „No results found").
   - Oben **Container** wählen und **Volltext-Filter** (regex) setzen.
2. Für **Live**-Mitlesen (z. B. während man eine Frage abschickt): **Dozzle**.

## Ad-hoc abfragen (Grafana → Explore, LogQL)

```logql
# Alle Fehler im ganzen Stack der letzten Stunde:
{container=~".+"} |~ `(?i)error|critical|traceback|exception`

# Nur die Websuche-Kette:
{container=~"open-webui|presidio_proxy|searxng"}

# Ein bestimmter Container, nur Warnungen aufwärts:
{container="searxng"} |~ `(?i)warn|error`

# Nach extrahiertem Level-Label filtern (sofern erkannt):
{container="open-webui", level=~"(?i)error"}
```

## Log-Level drehen

Die Such-/RAG-/Agent-Dienste stehen per Default auf **DEBUG** (siehe `.env`:
`OWUI_LOG_LEVEL`, `PRESIDIO_LOG_LEVEL`, `SEARXNG_DEBUG`, `AGENT_LOG_LEVEL`).
Leiser stellen → Wert auf `INFO`/`false` setzen und den jeweiligen Container neu
starten (`docker compose up -d <dienst>`). **vLLM-Dienste werden bewusst nicht
angefasst** (loggen ohnehin ausführlich).

## Hinweise

- Promtail erfasst **alle** Container auf dem Host, auch die des separaten
  RAGFlow-Compose und die vLLM-Dienste — genau das ist gewollt (eine Sammelstelle).
- Alles bleibt lokal: Loki/Grafana ohne Telemetrie, kein Egress (passt zur
  DSGVO-/Air-Gap-Linie des Stacks).
- Grafana läuft im lokalen Single-User-Modus (anonymer Admin-Zugriff wie
  `WEBUI_AUTH=false` bei OWUI). Soll ein Login erzwungen werden:
  `GF_AUTH_ANONYMOUS_ENABLED=false` in `docker-compose.observability.yml`.
