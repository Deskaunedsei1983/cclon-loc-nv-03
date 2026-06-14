FROM python:3.12-slim

# System-Libs fuer matplotlib/pillow/lxml etc.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libfreetype6-dev libpng-dev libjpeg-dev \
 && rm -rf /var/lib/apt/lists/*

# Non-root Sandbox-User
RUN useradd -m -u 1000 sandbox
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY run_api.py /app/run_api.py
COPY start.sh /app/start.sh
RUN chmod +x /app/start.sh \
 && mkdir -p /home/sandbox/work \
 && chown -R sandbox:sandbox /home/sandbox /app

USER sandbox
ENV SANDBOX_WORK=/home/sandbox/work

EXPOSE 8888 8000
CMD ["/app/start.sh"]
