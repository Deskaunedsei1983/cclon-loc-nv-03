"""
Microsandbox-microVM-Executor
=============================
Kleiner /run-Dienst mit DERSELBEN Schnittstelle wie der bisherige code-sandbox,
fuehrt den Code aber in einer hardware-isolierten Microsandbox-microVM (libkrun)
aus. Der Agent ruft diesen Dienst statt der schwaecheren Subprozess-Sandbox.

EMPFOHLEN: auf dem HOST betreiben (KVM/libkrun laufen dort am stabilsten,
rootless). Siehe README.md. Der Agent erreicht ihn dann ueber
http://host.docker.internal:8077/run.

[VERIFY] Die genauen SDK-Attribute (stdout_text/stderr/exit_code) und der
Server-/Verbindungsmodus koennen sich je Microsandbox-Version unterscheiden —
defensiv abgefragt; bei Bedarf anpassen (microsandbox v0.5.x).
"""

import os
import logging

from fastapi import FastAPI
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("msb-executor")

MSB_IMAGE = os.environ.get("MSB_IMAGE", "python")     # OCI-Image fuer die microVM
MSB_CPUS = int(os.environ.get("MSB_CPUS", "1"))
MSB_MEMORY = int(os.environ.get("MSB_MEMORY", "1024"))  # MB
RUN_TIMEOUT = int(os.environ.get("MSB_TIMEOUT", "120"))

app = FastAPI(title="Microsandbox microVM Executor")


class RunReq(BaseModel):
    code: str
    timeout: int | None = None


def _attr(obj, *names, default=""):
    for n in names:
        v = getattr(obj, n, None)
        if v is not None:
            return v() if callable(v) else v
    return default


@app.get("/healthz")
async def healthz():
    return {"ok": True, "engine": "microsandbox", "image": MSB_IMAGE}


@app.post("/run")
async def run(req: RunReq):
    # Import hier, damit der Dienst auch startet, wenn das SDK (noch) fehlt
    try:
        from microsandbox import Sandbox
    except Exception as e:
        return {"returncode": -1, "stdout": "",
                "stderr": f"microsandbox-SDK nicht installiert: {e}",
                "files": [], "work_dir": "microvm"}

    sandbox = None
    try:
        sandbox = await Sandbox.create(
            "agent-run", image=MSB_IMAGE, cpus=MSB_CPUS, memory=MSB_MEMORY,
        )
        # Code als python -c ausfuehren (ein Argument -> kein Shell-Quoting-Problem)
        out = await sandbox.exec("python", ["-c", req.code])
        stdout = _attr(out, "stdout_text", "stdout")
        stderr = _attr(out, "stderr_text", "stderr")
        rc = _attr(out, "exit_code", "returncode", default=0)
        return {"returncode": rc, "stdout": str(stdout)[-20000:],
                "stderr": str(stderr)[-8000:],
                "files": [],  # Datei-Rueckgabe aus der microVM nicht implementiert
                "work_dir": "microvm"}
    except Exception as e:
        log.exception("microVM-Lauf fehlgeschlagen")
        return {"returncode": -1, "stdout": "", "stderr": f"microVM-Fehler: {e}",
                "files": [], "work_dir": "microvm"}
    finally:
        if sandbox is not None:
            try:
                await sandbox.stop_and_wait()
            except Exception:
                pass
