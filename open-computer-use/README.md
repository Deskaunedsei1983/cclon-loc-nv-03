# Upgrade-Pfad B — Wide-Moat open-computer-use  [EXPERIMENTELL]

Gibt dem LLM einen **isolierten Ubuntu-Sandbox** mit Browser (Playwright/CDP),
Terminal, Code-Ausführung UND **Office-Skills (Word/Excel/PowerPoint/PDF)** sowie
autonomen Sub-Agenten. Steckbar in Open WebUI per MCP. Jede Session läuft in einem
eigenen Container; nichts leakt zwischen Sessions.

## Starten
```bash
docker compose --profile computer-use up -d
# Danach in OWUI als MCP-/Tool-Server einbinden (URL des Containers).
```

## SICHERHEITS-Hinweis (lies das!)
Der **Manager** spawnt pro Session Sandbox-Container und braucht dafür Zugriff auf
den **Docker-Socket** (`/var/run/docker.sock`). Das ist effektiv Root auf dem Host
— also eine echte **Vertrauensgrenze**, das Gegenteil von „isoliert".

Härtung (in Reihenfolge der Wirksamkeit):
- **Kata Containers** (Hypervisor-Isolation) via Kubernetes-Helm-Chart — stärkste Option.
- **gVisor** als Runtime auf Compose für die gespawnten Sandboxes.
- **Rootless Docker** + Socket-Proxy (nur benötigte API-Calls erlauben).
- Mindestens: dedizierte VM/Host für diesen Dienst, nicht auf dem Produktiv-Host.

Die Sandboxes haben per Default **Netzzugang** — für DSGVO Egress einschränken
oder über deinen Masking-Proxy leiten.

[VERIFY] Exaktes Image/Tag, ENV-Variablen und Ports gegen das aktuelle Repo prüfen:
https://github.com/Wide-Moat/open-computer-use  (die dortige Compose/Helm ist maßgeblich).
Die Definition in der root-`docker-compose.yml` ist ein funktionierendes Gerüst.
