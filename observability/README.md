# Zentrales Logging / Observability

Eine Sammelstelle für die Logs **aller** Container, damit nie wieder unklar ist,
„in welchem Container man schauen muss", wenn etwas klemmt (z. B. die Websuche
0 Treffer liefert).

## Bestandteile

| Dienst   | Aufgabe | Zugriff |
|----------|---------|---------|
| **promtail** | Liest via Docker-Socket (read-only) die Logs **jedes** laufenden Containers (Kernstack + RAGFlow + vLLM + Upgrades) und schickt sie an Loki. Keine Per-Container-Konfig. | – |
| **loki**     | Speichert alles durchsuchbar auf Platte (14 Tage Vorhaltung). | intern `:3100` |
| **grafana**  | Eine Web-UI: Volltext-/Level-/Zeit-Suche über den ganzen Stack, inkl. fertigem Dashboard. | http://localhost:3011 |
| **dozzle**   | Ultraleichter **Live**-Viewer aller Container-Logs (Echtzeit-Triage). | http://localhost:8085 |

Ports/Passwörter über die `.env` (`GRAFANA_PORT`, `GRAFANA_PASSWORD`, `DOZZLE_PORT`).

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
