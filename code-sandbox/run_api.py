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


def _files_root(session_id: str | None) -> pathlib.Path:
    """Wurzel fuer die DATEI-API (nicht fuer /run).

    Ohne Chat-ID einen LEEREN, versteckten Ordner statt des Sammelordners
    '_ohne_chat': OWUIs FileNav oeffnet sich in einem frisch angelegten Chat
    schon, bevor der Chat eine ID hat (`chatId` ist dann null, der Header
    X-Session-Id fehlt). Mit '_ohne_chat' als Wurzel saehe der Nutzer dort die
    Reste aller Anfragen ohne Zuordnung — ein leerer Ordner ist ehrlicher.
    Der Agent schreibt weiterhin nach '_ohne_chat', falls ihm die ID fehlt; das
    meldet er dann als Warnung im Log.
    """
    if not (session_id or "").strip():
        d = WORK / ".ohne_sitzung"
        d.mkdir(parents=True, exist_ok=True)
        return d
    return _chat_dir(session_id)


def _inside(child: pathlib.Path, parent: pathlib.Path) -> bool:
    return child == parent or str(child).startswith(str(parent) + os.sep)


def _virt(real: pathlib.Path, root: pathlib.Path) -> str:
    """Realer Pfad -> VIRTUELLER Pfad. '/' ist immer der Ordner DIESES Chats."""
    if real == root:
        return "/"
    try:
        return "/" + str(real.relative_to(root))
    except ValueError:
        return "/"


def _resolve(session_id: str | None, raw: str | None, *, must_exist: bool = False,
             scope: str = "session") -> pathlib.Path:
    """Virtuellen Client-Pfad in einen realen Pfad im Chat-Ordner aufloesen.

    Die API spricht nach aussen NUR virtuelle Pfade: '/' ist der Ordner dieses
    Chats, '/bericht.csv' eine Datei darin. Damit kann OWUIs Datei-Browser den
    Ordner gar nicht erst verlassen — es gibt keinen Namen fuer 'ausserhalb'.
    Das ist wichtig, weil FileNav.svelte den zuletzt betrachteten Pfad MODULWEIT
    speichert (`<script context="module"> let savedPath`) und ihn ueber
    Chatwechsel hinweg mitschleppt ("persist the current browsed path as the new
    session's cwd — don't re-fetch"). Frueher landete der Browser damit im
    Volume-Wurzelverzeichnis /home/sandbox/work.

    Alte absolute Container-Pfade werden noch akzeptiert (Prefix wird
    abgeschnitten), damit ein Browser-Tab mit altem Zustand nicht haengen bleibt.

    scope:
      'session' — muss im Chat-Ordner liegen, sonst 403 (alles Schreibende)
      'clamp'   — ausserhalb? Dann still auf den Chat-Ordner zurueckfallen
                  (Auflisten und cwd — dort waere ein 403 nur eine Sackgasse)
    """
    volume = pathlib.Path(os.path.realpath(WORK))
    root = pathlib.Path(os.path.realpath(_files_root(session_id)))
    p = (raw or "").strip()
    # Alt-Client: absoluter Container-Pfad -> auf den virtuellen Anteil kuerzen.
    for prefix in (str(root), str(volume)):
        if p == prefix:
            p = ""
            break
        if p.startswith(prefix + os.sep):
            p = p[len(prefix) + 1:]
            break
    p = p.strip().lstrip("/")
    if p in ("", ".", "./"):
        real = root
    else:
        real = pathlib.Path(os.path.realpath(root / p))

    if not _inside(real, root):
        if scope != "clamp":
            raise HTTPException(status_code=403, detail="Pfad ausserhalb des Chat-Ordners")
        real = root
    if must_exist and not real.exists():
        if scope != "clamp":
            raise HTTPException(status_code=404, detail="Nicht gefunden")
        # Auflisten darf nie in einer Sackgasse enden: ein Pfad aus einem
        # frueheren Chat existiert hier schlicht nicht -> Chat-Ordner zeigen.
        real = root
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


#  Nicht anzeigen: Arbeitsdateien, die der Nutzer nicht erzeugt hat.
#  'document.txt' ist die Textfassung des Uploads, die der Agent fuer den
#  Volltext-Modus in den Ordner schreibt — die ORIGINALDATEI liegt unter ihrem
#  echten Namen daneben, die Textfassung ist im Browser nur Rauschen.
HIDDEN_NAMES = {n.strip() for n in
                os.environ.get("SANDBOX_HIDE_NAMES", "document.txt,__pycache__").split(",")
                if n.strip()}


def _visible(p: pathlib.Path) -> bool:
    """Interne Dateien (.snippet_*.py, document.txt, __pycache__) nicht anzeigen."""
    return not p.name.startswith(".") and p.name not in HIDDEN_NAMES


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
    root = pathlib.Path(os.path.realpath(_files_root(session_id)))
    saved = _CWD.get(session_id or "")
    cwd = saved if saved and os.path.isdir(root / saved.lstrip("/")) else "/"
    # root.path='/' -> FileNav kann nicht hoeher navigieren als in diesen Chat.
    return {"cwd": cwd, "home": "/", "root": {"path": "/", "label": "Chat-Dateien"}}


@app.post("/files/cwd", include_in_schema=False)
async def files_setcwd(req: CwdReq, session_id: str = Depends(_auth)):
    # clamp: FileNav schiebt beim Anlegen eines Chats den Pfad des VORIGEN
    # Chats herueber — der landet dann still im richtigen Ordner.
    root = pathlib.Path(os.path.realpath(_files_root(session_id)))
    target = _resolve(session_id, req.path, scope="clamp")
    # Zeigt der Pfad ins Leere (Ordner eines anderen Chats, geloeschter Ordner),
    # NICHT den alten Stand behalten, sondern sauber auf den Chat-Ordner setzen.
    cwd = _virt(target, root) if target.is_dir() else "/"
    _CWD[session_id or ""] = cwd
    return {"cwd": cwd}


@app.get("/files/list", include_in_schema=False)
async def files_list(directory: str = "/", session_id: str = Depends(_auth)):
    root = pathlib.Path(os.path.realpath(_files_root(session_id)))
    target = _resolve(session_id, directory, must_exist=True, scope="clamp")
    if not target.is_dir():
        raise HTTPException(status_code=400, detail="Kein Verzeichnis")
    entries = [_entry(p) for p in target.iterdir() if _visible(p)]
    entries.sort(key=lambda e: (e["type"] != "directory", e["name"].lower()))
    return {"path": _virt(target, root), "entries": entries}


@app.get("/files/read", include_in_schema=False)
async def files_read(path: str, session_id: str = Depends(_auth)):
    root = pathlib.Path(os.path.realpath(_files_root(session_id)))
    target = _resolve(session_id, path, must_exist=True)
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
    return {"path": _virt(target, root), "total_lines": text.count("\n") + 1, "content": text}


@app.get("/files/view", include_in_schema=False)
async def files_view(path: str, session_id: str = Depends(_auth)):
    target = _resolve(session_id, path, must_exist=True)
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
    targets = [_resolve(session_id, p, must_exist=True) for p in (req.paths or [])]
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
    root = pathlib.Path(os.path.realpath(_files_root(session_id)))
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
    # real_path: fuer den Agenten (Volume-Pfad), path: virtuell fuer FileNav.
    return {"path": _virt(dest, root), "real_path": str(dest), "size": size}


class PathReq(BaseModel):
    path: str


@app.post("/files/mkdir", include_in_schema=False)
async def files_mkdir(req: PathReq, session_id: str = Depends(_auth)):
    root = pathlib.Path(os.path.realpath(_files_root(session_id)))
    target = _resolve(session_id, req.path)
    target.mkdir(parents=True, exist_ok=True)
    return {"path": _virt(target, root)}


@app.delete("/files/delete", include_in_schema=False)
async def files_delete(path: str, session_id: str = Depends(_auth)):
    root = pathlib.Path(os.path.realpath(_files_root(session_id)))
    target = _resolve(session_id, path, must_exist=True)
    if target == root:
        raise HTTPException(status_code=400, detail="Der Chat-Ordner selbst wird nicht geloescht")
    if target.is_dir():
        shutil.rmtree(target)
        return {"path": _virt(target, root), "type": "directory"}
    target.unlink()
    return {"path": _virt(target, root), "type": "file"}


class MoveReq(BaseModel):
    source: str
    destination: str


@app.post("/files/move", include_in_schema=False)
async def files_move(req: MoveReq, session_id: str = Depends(_auth)):
    root = pathlib.Path(os.path.realpath(_files_root(session_id)))
    src = _resolve(session_id, req.source, must_exist=True)
    dst = _resolve(session_id, req.destination)
    if dst.is_dir():
        dst = dst / src.name
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    return {"source": _virt(src, root), "destination": _virt(dst, root)}


@app.get("/ports", include_in_schema=False)
async def ports(session_id: str = Depends(_auth)):
    # Die Sandbox hat kein Netz -> es gibt nichts zu proxyen. Leere Liste, damit
    # OWUIs Port-Panel nicht in einen Fehler laeuft.
    return {"ports": []}


@app.exception_handler(HTTPException)
async def _http_error(request: Request, exc: HTTPException):
    # FileNav erwartet bei Fehlern ein JSON-Objekt (es liest res.json()).
    return JSONResponse({"error": exc.detail, "detail": exc.detail}, status_code=exc.status_code)
