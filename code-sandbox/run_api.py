"""
Code-Sandbox /run-API
=====================
Minimaler, isolierter Python-Executor fuer den Agent (Tool "run_code").
Laeuft im luftdichten Container (sandbox-net, kein Internet), als non-root,
mit Timeout und in einem Arbeitsverzeichnis. Vom LLM erzeugte Dateien
(.docx/.xlsx/.pptx/.ipynb/Plots/...) landen in /home/sandbox/work und werden
(bis zu einer Groessengrenze) base64-kodiert zurueckgegeben.

Sicherheit: Der Container hat KEINEN Netzzugang und harte Ressourcenlimits.
Fuer Hypervisor-Isolation zusaetzlich gVisor (runtime: runsc) aktivieren.

OWUI nutzt NICHT diese API, sondern direkt den Jupyter-Server (Port 8888).
"""

import os
import sys
import base64
import subprocess
import tempfile
import pathlib
import time

from fastapi import FastAPI
from pydantic import BaseModel

WORK = pathlib.Path(os.environ.get("SANDBOX_WORK", "/home/sandbox/work"))
WORK.mkdir(parents=True, exist_ok=True)
TIMEOUT = int(os.environ.get("SANDBOX_TIMEOUT", "120"))
MAX_FILE_BYTES = int(os.environ.get("SANDBOX_MAX_FILE_BYTES", str(8 * 1024 * 1024)))

app = FastAPI(title="Code Sandbox /run")


class RunReq(BaseModel):
    code: str
    timeout: int | None = None
    files: dict[str, str] | None = None  # {name: base64} -> vor dem Lauf ins run_dir


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.post("/run")
async def run(req: RunReq):
    # Eigenes Unterverzeichnis pro Lauf -> neue Dateien sind leicht erkennbar.
    run_dir = pathlib.Path(tempfile.mkdtemp(prefix="run_", dir=WORK))
    script = run_dir / "snippet.py"
    script.write_text(req.code, encoding="utf-8")

    # Eingabedateien (z.B. Volltext 'document.txt') ins Arbeitsverzeichnis schreiben.
    input_names = set()
    for fname, b64 in (req.files or {}).items():
        safe = pathlib.Path(fname).name  # kein Pfad-Ausbruch
        try:
            (run_dir / safe).write_bytes(base64.b64decode(b64))
            input_names.add(safe)
        except Exception:
            pass

    t0 = time.time()
    try:
        proc = subprocess.run(
            [sys.executable, str(script)],
            cwd=str(run_dir),
            capture_output=True,
            text=True,
            timeout=req.timeout or TIMEOUT,
        )
        stdout, stderr, rc = proc.stdout, proc.stderr, proc.returncode
    except subprocess.TimeoutExpired:
        stdout, stderr, rc = "", f"[Timeout nach {req.timeout or TIMEOUT}s]", -1

    # Erzeugte Dateien einsammeln (ausser dem Snippet selbst).
    files = []
    for p in sorted(run_dir.rglob("*")):
        if p.is_file() and p.name != "snippet.py" and p.name not in input_names:
            size = p.stat().st_size
            entry = {"name": p.name, "size": size, "path": str(p)}
            if size <= MAX_FILE_BYTES:
                entry["base64"] = base64.b64encode(p.read_bytes()).decode("ascii")
            else:
                entry["note"] = "zu gross fuer Inline-Rueckgabe; liegt im Sandbox-Volume"
            files.append(entry)

    return {
        "returncode": rc,
        "stdout": stdout[-20000:],
        "stderr": stderr[-8000:],
        "files": files,
        "elapsed_s": round(time.time() - t0, 2),
        "work_dir": str(run_dir),
    }
