"""
Code-Sandbox: /run-API  +  Open-Terminal-kompatible Datei-API
=============================================================
Minimaler, isolierter Python-Executor fuer den Agent (Tool "run_code").
Laeuft im luftdichten Container (sandbox-net, kein Internet), als non-root,
mit Timeout und in einem Arbeitsverzeichnis PRO CHAT.

Zwei Aufgaben:

1) POST /run     — Code ausfuehren (Agent-Tool "run_code").
   Arbeitsverzeichnis ist der Chat-Ordner  <WORK>/chat_<chat_id>/ ,
   d.h. ein spaeterer Lauf im SELBEN Chat sieht die Dateien des frueheren
   Laufs (iteratives Arbeiten: erzeugen -> korrigieren -> ergaenzen).
   Neu entstandene/geaenderte Dateien werden erkannt (Snapshot-Vergleich)
   und - bis zu einer Groessengrenze - base64 zurueckgegeben.

2) /files/*      — Datei-Browser fuer Open WebUI (rechte Seitenleiste).
   OWUI 0.11 zeigt den Datei-Browser (ChatControls -> Reiter "Files",
   Komponente FileNav.svelte) nur fuer einen konfigurierten TERMINAL-SERVER.
   Dieser Dienst spielt genau diesen Terminal-Server — aber NUR den
   Datei-Teil: /api/config meldet  features.terminal = false , damit OWUI
   gar keine Shell anbietet (kein PTY, kein WebSocket). Damit bleibt die
   Sandbox luftdicht und der Nutzer bekommt trotzdem die Seitenleiste mit
   Vorschau (docx/xlsx/pptx/pdf/ipynb/csv/Bilder) und Download/ZIP.

   OWUI ruft NICHT direkt hierher: der Browser spricht mit OWUI
   (/api/v1/terminals/<id>/...), OWUI proxyt serverseitig hierher und
   schickt dabei
     Authorization: Bearer <key aus der Terminal-Server-Konfiguration>
     X-Session-Id: <chat_id>            (FileNav reicht die Chat-ID durch)
   -> die Chat-ID trennt die Ordner: jeder Chat sieht NUR seine Dateien.

Sicherheit
----------
* Kein Netzzugang, harte Ressourcenlimits; optional gVisor (runtime: runsc).
* Die Datei-API ist ohne SANDBOX_FILES_TOKEN komplett AUS (401).
* Jeder Pfad wird per realpath gegen den Chat-Ordner geprueft (kein Ausbruch).
* Die Endpunkte stehen bewusst NICHT im OpenAPI-Schema: OWUI liest von
  Terminal-Servern sonst /openapi.json und wuerde dem Modell Tools wie
  "Datei loeschen" anbieten. Der Datei-Browser braucht das Schema nicht.

OWUI nutzt fuer den Code-Interpreter weiterhin den Jupyter-Server (Port 8888).
"""

import base64
import hmac
import io
import mimetypes
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time
import uuid
import zipfile

from fastapi import Depends, FastAPI, HTTPException, Request, UploadFile, File
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel

WORK = pathlib.Path(os.environ.get("SANDBOX_WORK", "/home/sandbox/work"))
WORK.mkdir(parents=True, exist_ok=True)
TIMEOUT = int(os.environ.get("SANDBOX_TIMEOUT", "120"))
MAX_FILE_BYTES = int(os.environ.get("SANDBOX_MAX_FILE_BYTES", str(8 * 1024 * 1024)))

# Datei-API (OWUI-Seitenleiste). Leer -> API aus. Muss mit dem 'key' der
# Terminal-Server-Konfiguration in OWUI uebereinstimmen.
FILES_TOKEN = os.environ.get("SANDBOX_FILES_TOKEN", "").strip()
# Obergrenze fuer die TEXT-Vorschau (/files/read). Downloads sind unbegrenzt.
READ_MAX_BYTES = int(os.environ.get("SANDBOX_READ_MAX_BYTES", str(2 * 1024 * 1024)))
# Uploads in den Chat-Ordner (Drag&Drop in der Seitenleiste).
UPLOAD_MAX_BYTES = int(os.environ.get("SANDBOX_UPLOAD_MAX_BYTES", str(512 * 1024 * 1024)))

app = FastAPI(title="Code Sandbox")

# Merkt sich das zuletzt geoeffnete Verzeichnis je Chat (FileNav ruft dafuer
# POST /files/cwd). Nur im RAM — nach einem Neustart landet man wieder oben.
_CWD: "dict[str, str]" = {}

_ID_RE = re.compile(r"[^A-Za-z0-9_.-]")


# --- Chat-Ordner + Pfad-Haertung --------------------------------------------
def _chat_dir(session_id: str | None) -> pathlib.Path:
    """<WORK>/chat_<id> — pro Chat ein Ordner. Ohne ID ein Sammelordner."""
    safe = _ID_RE.sub("", (session_id or ""))[:64]
    d = WORK / (f"chat_{safe}" if safe else "_ohne_chat")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _inside(child: pathlib.Path, parent: pathlib.Path) -> bool:
    return child == parent or str(child).startswith(str(parent) + os.sep)


def _resolve(session_id: str | None, raw: str | None, *, must_exist: bool = False,
             scope: str = "session") -> pathlib.Path:
    """Einen vom Client gelieferten Pfad aufloesen und haerten.

    scope steuert, wie streng geprueft wird:
      'session' — muss IM Chat-Ordner liegen (alles Schreibende)
      'volume'  — irgendwo unterhalb des Sandbox-Volumes (Lesen/Download;
                  der Pfad stammt ohnehin aus einer vorherigen Auflistung)
      'clamp'   — ausserhalb des Chat-Ordners? Dann still auf den Chat-Ordner
                  zurueckfallen statt 403.

    'clamp' ist noetig, weil OWUIs FileNav den zuletzt betrachteten Pfad
    MODULWEIT speichert (`let savedPath` in FileNav.svelte) und ihn beim
    Anlegen eines Chats mit der NEUEN Chat-ID weiterreicht
    ("Chat just got created (null -> real ID): persist the current browsed
    path as the new session's cwd — don't re-fetch"). Der Pfad gehoert dann
    noch zum vorigen Chat. Ein 403 waere hier eine Sackgasse; wir schwenken
    stattdessen auf den richtigen Ordner.
    """
    volume = pathlib.Path(os.path.realpath(WORK))
    root = pathlib.Path(os.path.realpath(_chat_dir(session_id)))
    p = (raw or "").strip()
    if not p or p in ("/", ".", "./"):
        target = root
    else:
        cand = pathlib.Path(p)
        target = cand if cand.is_absolute() else (root / p)
    real = pathlib.Path(os.path.realpath(target))

    if not _inside(real, volume):
        raise HTTPException(status_code=403, detail="Pfad ausserhalb des Sandbox-Volumes")
    if scope != "volume" and not _inside(real, root):
        if scope == "clamp":
            real = root
        else:
            raise HTTPException(status_code=403, detail="Pfad ausserhalb des Chat-Ordners")
    if must_exist and not real.exists():
        raise HTTPException(status_code=404, detail="Nicht gefunden")
    return real


def _auth(request: Request) -> str:
    """Bearer-Token pruefen; liefert die Chat-ID (X-Session-Id) zurueck."""
    if not FILES_TOKEN:
        raise HTTPException(status_code=401, detail="Datei-API deaktiviert (SANDBOX_FILES_TOKEN fehlt)")
    tok = (request.headers.get("authorization") or "").strip()
    if tok.lower().startswith("bearer "):
        tok = tok[7:].strip()
    if not hmac.compare_digest(tok, FILES_TOKEN):
        raise HTTPException(status_code=401, detail="Nicht autorisiert")
    return (request.headers.get("x-session-id") or "").strip()


def _visible(p: pathlib.Path) -> bool:
    """Interne Dateien (.snippet_*.py, __pycache__, ...) nicht anzeigen."""
    return not p.name.startswith(".") and p.name != "__pycache__"


def _entry(p: pathlib.Path) -> dict:
    try:
        st = p.stat()
    except OSError:
        return {"name": p.name, "type": "file", "size": 0, "modified": 0}
    return {
        "name": p.name,
        "type": "directory" if p.is_dir() else "file",
        "size": 0 if p.is_dir() else st.st_size,
        "modified": int(st.st_mtime),
    }


def _media_type(name: str) -> str:
    """Bilder/PDF mit echtem Typ (Vorschau in OWUI), Rest als Download-Strom.
    Hinweis: OWUIs Terminal-Proxy STREAMT nur 'image/', 'application/pdf' und
    'application/octet-stream' — alles andere wird gepuffert."""
    guess = mimetypes.guess_type(name)[0] or ""
    if guess.startswith(("image/", "video/", "audio/")) or guess == "application/pdf":
        return guess
    return "application/octet-stream"


# --- /run --------------------------------------------------------------------
class RunReq(BaseModel):
    code: str
    timeout: int | None = None
    files: dict[str, str] | None = None  # {name: base64} -> vor dem Lauf ins Arbeitsverzeichnis
    chat_id: str | None = None           # trennt die Arbeitsordner je Chat


@app.get("/healthz")
async def healthz():
    return {"ok": True}


def _snapshot(root: pathlib.Path) -> dict:
    """(Pfad -> mtime_ns, groesse) aller sichtbaren Dateien — fuer den Vergleich
    'was hat dieser Lauf erzeugt oder geaendert?'."""
    snap = {}
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(root)
        if any(part.startswith(".") or part == "__pycache__" for part in rel.parts):
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        snap[str(p)] = (st.st_mtime_ns, st.st_size)
    return snap


@app.post("/run")
async def run(req: RunReq):
    # Arbeitsverzeichnis = Chat-Ordner. So sieht ein Folgelauf die Dateien des
    # vorherigen Laufs (iteratives Arbeiten) und die OWUI-Seitenleiste zeigt
    # genau diesen Ordner an.
    run_dir = _chat_dir(req.chat_id)
    script = run_dir / f".snippet_{uuid.uuid4().hex[:8]}.py"
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

    before = _snapshot(run_dir)

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
    finally:
        script.unlink(missing_ok=True)

    # Neu entstandene ODER geaenderte Dateien einsammeln (Eingaben ausgenommen).
    files = []
    for path_str, meta in sorted(_snapshot(run_dir).items()):
        p = pathlib.Path(path_str)
        if p.name in input_names or before.get(path_str) == meta:
            continue
        size = meta[1]
        entry = {"name": p.name, "size": size, "path": path_str}
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


# --- Open-Terminal-Datei-API (OWUI-Seitenleiste) -----------------------------
# Alle Endpunkte absichtlich ohne OpenAPI-Schema (siehe Modul-Docstring).

@app.get("/api/config", include_in_schema=False)
async def terminal_config(session_id: str = Depends(_auth)):
    # terminal=false -> OWUI blendet die Shell aus und zeigt nur den Datei-Browser.
    return {"features": {"terminal": False}}


class CwdReq(BaseModel):
    path: str | None = None


@app.get("/files/cwd", include_in_schema=False)
async def files_cwd(session_id: str = Depends(_auth)):
    root = _chat_dir(session_id)
    saved = _CWD.get(session_id or "")
    cwd = saved if saved and saved.startswith(str(root)) and os.path.isdir(saved) else str(root)
    return {
        "cwd": cwd,
        "home": str(root),
        "root": {"path": str(root), "label": "Chat-Dateien"},
    }


@app.post("/files/cwd", include_in_schema=False)
async def files_setcwd(req: CwdReq, session_id: str = Depends(_auth)):
    # clamp: FileNav schiebt beim Anlegen eines Chats den Pfad des VORIGEN
    # Chats herueber — der landet dann still im richtigen Ordner.
    target = _resolve(session_id, req.path, scope="clamp")
    if target.is_dir():
        _CWD[session_id or ""] = str(target)
    return {"cwd": str(target)}


@app.get("/files/list", include_in_schema=False)
async def files_list(directory: str = "/", session_id: str = Depends(_auth)):
    target = _resolve(session_id, directory, must_exist=True, scope="clamp")
    if not target.is_dir():
        raise HTTPException(status_code=400, detail="Kein Verzeichnis")
    entries = [_entry(p) for p in target.iterdir() if _visible(p)]
    entries.sort(key=lambda e: (e["type"] != "directory", e["name"].lower()))
    return {"path": str(target), "entries": entries}


@app.get("/files/read", include_in_schema=False)
async def files_read(path: str, session_id: str = Depends(_auth)):
    # 'volume': der Pfad stammt aus einer vorherigen Auflistung; ein 403 nach
    # einem Chatwechsel waere nur eine Sackgasse. Schreiben bleibt Chat-lokal.
    target = _resolve(session_id, path, must_exist=True, scope="volume")
    if target.is_dir():
        raise HTTPException(status_code=400, detail="Ist ein Verzeichnis")
    if target.stat().st_size > READ_MAX_BYTES:
        raise HTTPException(status_code=413, detail="Datei zu gross fuer die Vorschau")
    raw = target.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        # OWUI erkennt Binaerdateien am Content-Type und zeigt einen Platzhalter.
        return Response(content=b"", media_type="application/octet-stream")
    return {"path": str(target), "total_lines": text.count("\n") + 1, "content": text}


@app.get("/files/view", include_in_schema=False)
async def files_view(path: str, session_id: str = Depends(_auth)):
    target = _resolve(session_id, path, must_exist=True, scope="volume")
    if target.is_dir():
        raise HTTPException(status_code=400, detail="Ist ein Verzeichnis")
    size = target.stat().st_size

    def _stream():
        with open(target, "rb") as fh:
            while chunk := fh.read(1024 * 1024):
                yield chunk

    return StreamingResponse(
        _stream(),
        media_type=_media_type(target.name),
        headers={"Content-Disposition": f'attachment; filename="{target.name}"',
                 "Content-Length": str(size)},
    )


class ArchiveReq(BaseModel):
    paths: list[str]


@app.post("/files/archive", include_in_schema=False)
async def files_archive(req: ArchiveReq, session_id: str = Depends(_auth)):
    """Mehrere Dateien/Ordner als ZIP — so laedt man in OWUI eine Auswahl auf
    einmal herunter (der Grund, warum viele Dateien in einem Chat frueher
    unuebersichtlich waren)."""
    targets = [_resolve(session_id, p, must_exist=True, scope="volume") for p in (req.paths or [])]
    if not targets:
        raise HTTPException(status_code=400, detail="Keine Pfade")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for t in targets:
            if t.is_dir():
                for p in sorted(t.rglob("*")):
                    if p.is_file() and _visible(p):
                        zf.write(p, arcname=str(pathlib.Path(t.name) / p.relative_to(t)))
            else:
                zf.write(t, arcname=t.name)
    buf.seek(0)
    name = f"{targets[0].name}.zip" if len(targets) == 1 else "dateien.zip"
    return StreamingResponse(
        buf,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@app.post("/files/upload", include_in_schema=False)
async def files_upload(directory: str = "/", file: UploadFile = File(...),
                       session_id: str = Depends(_auth)):
    target_dir = _resolve(session_id, directory, must_exist=True)
    if not target_dir.is_dir():
        raise HTTPException(status_code=400, detail="Kein Verzeichnis")
    name = pathlib.Path(file.filename or "datei").name
    dest = target_dir / name
    size = 0
    with open(dest, "wb") as fh:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > UPLOAD_MAX_BYTES:
                fh.close()
                dest.unlink(missing_ok=True)
                raise HTTPException(status_code=413, detail="Datei zu gross")
            fh.write(chunk)
    return {"path": str(dest), "size": size}


class PathReq(BaseModel):
    path: str


@app.post("/files/mkdir", include_in_schema=False)
async def files_mkdir(req: PathReq, session_id: str = Depends(_auth)):
    target = _resolve(session_id, req.path)
    target.mkdir(parents=True, exist_ok=True)
    return {"path": str(target)}


@app.delete("/files/delete", include_in_schema=False)
async def files_delete(path: str, session_id: str = Depends(_auth)):
    target = _resolve(session_id, path, must_exist=True)
    if target == _resolve(session_id, "/"):
        raise HTTPException(status_code=400, detail="Der Chat-Ordner selbst wird nicht geloescht")
    if target.is_dir():
        shutil.rmtree(target)
        return {"path": str(target), "type": "directory"}
    target.unlink()
    return {"path": str(target), "type": "file"}


class MoveReq(BaseModel):
    source: str
    destination: str


@app.post("/files/move", include_in_schema=False)
async def files_move(req: MoveReq, session_id: str = Depends(_auth)):
    src = _resolve(session_id, req.source, must_exist=True)
    dst = _resolve(session_id, req.destination)
    if dst.is_dir():
        dst = dst / src.name
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    return {"source": str(src), "destination": str(dst)}


@app.get("/ports", include_in_schema=False)
async def ports(session_id: str = Depends(_auth)):
    # Die Sandbox hat kein Netz -> es gibt nichts zu proxyen. Leere Liste, damit
    # OWUIs Port-Panel nicht in einen Fehler laeuft.
    return {"ports": []}


@app.exception_handler(HTTPException)
async def _http_error(request: Request, exc: HTTPException):
    # FileNav erwartet bei Fehlern ein JSON-Objekt (es liest res.json()).
    return JSONResponse({"error": exc.detail, "detail": exc.detail}, status_code=exc.status_code)
