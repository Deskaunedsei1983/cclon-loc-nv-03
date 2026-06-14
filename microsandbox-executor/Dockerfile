# [EXPERIMENTELL] Container-Variante des microVM-Executors.
# microVMs/libkrun laufen am stabilsten auf dem HOST (siehe README.md).
# Dieser Container braucht zur Laufzeit /dev/kvm + NET_ADMIN (in der Overlay-Compose gesetzt)
# und ggf. die Microsandbox-Runtime/libkrun im Image.
FROM python:3.12-slim

# Microsandbox-Runtime (libkrun, Kernel) installieren — [VERIFY] Methode/Abhaengigkeiten
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
 && (curl -sSL https://get.microsandbox.dev | sh || echo "[WARN] msb-Runtime-Install fehlgeschlagen; ggf. Host-Betrieb nutzen") \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY run_api.py .

EXPOSE 8077
CMD ["uvicorn", "run_api:app", "--host", "0.0.0.0", "--port", "8077"]
