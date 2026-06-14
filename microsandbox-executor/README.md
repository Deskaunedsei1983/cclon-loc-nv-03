# microVM-Executor (Microsandbox) — ersetzt E2B/Fragments-Sandbox

Hardware-isolierte Code-Ausführung für den **Agent**: läuft in
[Microsandbox](https://microsandbox.dev)-microVMs (libkrun/KVM, Apache-2.0,
**lokal & air-gappable, keine Telemetrie**). Stärker isoliert als der bisherige
Subprozess-`code-sandbox`. Stellt eine `/run`-API bereit (gleiche Schnittstelle
wie zuvor) — der Agent ruft sie über `MSB_EXECUTOR_URL`.

> Der Jupyter-`code-sandbox` bleibt für OWUIs Office-Datei-Erzeugung bestehen.
> Dieser Executor ist für die Code-Ausführung des Agents (z.B. im Critic-Loop).

## Empfohlen: HOST-Betrieb (am stabilsten für KVM, rootless)

```bash
# 1) KVM verfügbar? (Blackwell/CachyOS: ja)
ls -l /dev/kvm && groups | grep -q kvm || sudo usermod -aG kvm "$USER"   # danach neu einloggen

# 2) Microsandbox-Runtime installieren (libkrun + Kernel)
curl -sSL https://get.microsandbox.dev | sh

# 3) Diesen Executor als kleinen Host-Dienst starten
cd microsandbox-executor
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn run_api:app --host 0.0.0.0 --port 8077        # -> http://localhost:8077/run
```

Optional als systemd-Dienst:
```ini
# /etc/systemd/system/msb-executor.service
[Unit]
Description=Microsandbox microVM Executor
After=network.target
[Service]
User=%i
WorkingDirectory=/pfad/zu/microsandbox-executor
ExecStart=/pfad/zu/microsandbox-executor/.venv/bin/uvicorn run_api:app --host 0.0.0.0 --port 8077
Restart=on-failure
[Install]
WantedBy=multi-user.target
```

Der Agent (im Container) erreicht den Host-Dienst über
`MSB_EXECUTOR_URL=http://host.docker.internal:8077/run` (so im Compose voreingestellt).

## Alternative: Container-Betrieb (experimentell)
```bash
./start.sh microvm     # bzw. docker compose -f ... -f docker-compose.upgrades.yml --profile microvm up -d
# dann im Agent: MSB_EXECUTOR_URL=http://microsandbox-executor:8077/run
```
Braucht `/dev/kvm` + `NET_ADMIN` (in der Overlay-Compose gesetzt) und ggf.
`/dev/net/tun`. microVMs-in-Docker sind heikler als der Host-Betrieb — wenn es
zickt, nimm den Host-Weg oben.

## Office-Dateien im microVM
Der Default nutzt das OCI-Image `python` (nur Standardbibliothek + das, was das
Image mitbringt). Für `.docx/.xlsx/.pptx` aus dem Agent heraus ein eigenes Image
mit `python-docx/openpyxl/python-pptx` bauen und `MSB_IMAGE` darauf setzen
(Microsandbox zieht OCI-Images aus jeder Registry). Für die Nutzer-Workflows in
OWUI bleibt ohnehin der Jupyter-`code-sandbox` zuständig.

## Sicherheit / DSGVO
Microsandbox injiziert Secrets über die Netzwerkschicht (Platzhalter im Gast,
echter Wert nur bei TLS-Handshake zu erlaubten Hosts), erlaubt Host-Allowlists
und DNS-Pinning, sendet keine Telemetrie. Für strikte Air-Gap die microVM-
Netzwerkfreigabe einschränken und benötigte OCI-Images vorab `msb pull`en.

[VERIFY] SDK-Verbindungs-/Server-Modell und Output-Attribute (stdout_text/exit_code)
gegen die installierte Microsandbox-Version prüfen (Doku: https://docs.microsandbox.dev).
