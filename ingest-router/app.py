"""
ingest-router — intelligente Upload-Weiche: RAGFlow  ODER  Morphik
==================================================================
Eine Datei kommt rein (von Open WebUI ODER per curl) -> der Router KLASSIFIZIERT
sie und leitet sie an das passende RAG-Backend weiter:

  * BILD-/SCAN-/TABELLEN-lastig  -> Morphik   (visuelles/multimodales RAG, ColPali)
  * sonst (Text/Office)          -> RAGFlow   (klassisches Text-RAG)

So landet jedes Dokument dort, wo es am besten verarbeitet wird — ohne dass der
Nutzer etwas waehlen muss. Die Klassifikation ist erklaerbar (jede Entscheidung
wird mit BEGRUENDUNG geloggt und in der Antwort zurueckgegeben) und per ENV justierbar.

Endpoints:
  GET  /healthz            -> Status + erkannte Backends
  POST /classify  (file)   -> Trockenlauf: nur Entscheidung + Begruendung, kein Ingest
  POST /ingest    (file)   -> klassifizieren UND ins gewaehlte Backend hochladen

[VERIFY] Die Ingest-Endpunkte von RAGFlow (Upload + Parse) und Morphik (/ingest/file)
sind versionsabhaengig — unten klar markiert. Gegen deine Versionen pruefen.
"""

from __future__ import annotations

import io
import json
import logging
import os

import httpx
from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import JSONResponse

_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=getattr(logging, _LEVEL, logging.INFO))
log = logging.getLogger("ingest-router")

# --- Backends ---------------------------------------------------------------
RAGFLOW_API_URL = os.environ.get("RAGFLOW_API_URL", "").rstrip("/")
RAGFLOW_API_KEY = os.environ.get("RAGFLOW_API_KEY", "")
# Ziel-Dataset (Knowledge Base) fuer Text-Uploads. Ohne dieses kann RAGFlow nicht
# einsortieren -> dann faellt der Router fuer Text auf "kein Ziel" zurueck.
RAGFLOW_INGEST_DATASET_ID = os.environ.get("RAGFLOW_INGEST_DATASET_ID", "").strip()

MORPHIK_API_URL = os.environ.get("MORPHIK_API_URL", "").rstrip("/")
MORPHIK_API_KEY = os.environ.get("MORPHIK_API_KEY", "")

# --- Klassifikations-Stellschrauben (ENV) -----------------------------------
# PDF mit weniger als X extrahierten Zeichen/Seite gilt als Scan/Bild -> Morphik.
PDF_TEXT_MIN_CHARS_PER_PAGE = int(os.environ.get("PDF_TEXT_MIN_CHARS_PER_PAGE", "200"))
# Finden sich auf >= diesem Anteil der gepruefen Seiten Tabellen -> Morphik.
PDF_TABLE_PAGE_RATIO = float(os.environ.get("PDF_TABLE_PAGE_RATIO", "0.34"))
# Wie viele Seiten am Anfang stichprobenartig untersucht werden.
MAX_SAMPLE_PAGES = int(os.environ.get("MAX_SAMPLE_PAGES", "5"))
# Tabellenkalkulationen (xlsx/csv/...) als "tabellenlastig" an Morphik? (sonst RAGFlow)
SPREADSHEET_TO_MORPHIK = os.environ.get("SPREADSHEET_TO_MORPHIK", "true").strip().lower() in (
    "1", "true", "yes", "on",
)
# Ist Morphik NICHT konfiguriert (Profil aus), Morphik-Ziele auf RAGFlow umlenken,
# statt den Upload zu verlieren (RAGFlows DeepDoc kann Scans/Tabellen auch parsen).
INGEST_FALLBACK_TO_RAGFLOW = os.environ.get("INGEST_FALLBACK_TO_RAGFLOW", "true").strip().lower() in (
    "1", "true", "yes", "on",
)

RAGFLOW = "ragflow"
MORPHIK = "morphik"

IMAGE_EXT = {"png", "jpg", "jpeg", "tif", "tiff", "bmp", "webp", "gif", "heic", "heif"}
SPREADSHEET_EXT = {"xlsx", "xls", "xlsm", "ods", "csv", "tsv"}

app = FastAPI(title="ingest-router (RAGFlow ODER Morphik)")


# ===========================================================================
#  KLASSIFIKATION  — erklaerbar, ENV-justierbar
# ===========================================================================
def _ext(filename: str) -> str:
    return filename.rsplit(".", 1)[-1].lower() if "." in (filename or "") else ""


def _classify_pdf(data: bytes) -> tuple[str, str]:
    """PDF genauer ansehen: text-arm (Scan/Bild) ODER tabellenlastig -> Morphik."""
    # 1) Textdichte ueber pypdf (leicht, ohne Render).
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        pages = reader.pages[:MAX_SAMPLE_PAGES]
        if pages:
            total = sum(len((p.extract_text() or "").strip()) for p in pages)
            per_page = total / len(pages)
            if per_page < PDF_TEXT_MIN_CHARS_PER_PAGE:
                return MORPHIK, (f"PDF text-arm ({per_page:.0f} Zeichen/Seite < "
                                 f"{PDF_TEXT_MIN_CHARS_PER_PAGE}) -> Scan/Bild")
    except Exception:
        log.warning("PDF-Textdichte nicht bestimmbar (pypdf) -> weiter mit Tabellen-Check")

    # 2) Tabellen-Erkennung (best effort, pdfplumber). Fehlt es, ueberspringen.
    try:
        import pdfplumber

        with pdfplumber.open(io.BytesIO(data)) as pdf:
            sample = pdf.pages[:MAX_SAMPLE_PAGES]
            if sample:
                with_tables = sum(1 for pg in sample if pg.find_tables())
                ratio = with_tables / len(sample)
                if ratio >= PDF_TABLE_PAGE_RATIO:
                    return MORPHIK, (f"PDF tabellenlastig (Tabellen auf {ratio:.0%} der "
                                     f"Stichprobe >= {PDF_TABLE_PAGE_RATIO:.0%})")
    except Exception:
        log.warning("PDF-Tabellen-Check uebersprungen (pdfplumber fehlt/fehlerhaft)")

    return RAGFLOW, "PDF textbasiert -> RAGFlow"


def classify(filename: str, content_type: str, data: bytes) -> tuple[str, str]:
    """Liefert (backend, begruendung). Reine Heuristik, keine Netz-Calls."""
    ext = _ext(filename)
    ct = (content_type or "").lower()

    if ct.startswith("image/") or ext in IMAGE_EXT:
        return MORPHIK, f"Bilddatei ({ext or ct}) -> Morphik (visuelles RAG)"

    if ext in SPREADSHEET_EXT or "spreadsheet" in ct or ct in ("text/csv", "text/tab-separated-values"):
        target = MORPHIK if SPREADSHEET_TO_MORPHIK else RAGFLOW
        return target, f"Tabellendokument ({ext or ct}) -> {target}"

    if ext == "pdf" or ct == "application/pdf":
        return _classify_pdf(data)

    # Alles andere (docx/pptx/txt/md/html/eml/json ...) ist text-/dokumentlastig.
    return RAGFLOW, f"Text-/Office-Dokument ({ext or ct or 'unbekannt'}) -> RAGFlow"


# ===========================================================================
#  INGEST  — RAGFlow (Upload + Parse)  bzw.  Morphik (/ingest/file)
# ===========================================================================
async def _to_ragflow(http: httpx.AsyncClient, filename: str, content_type: str, data: bytes) -> dict:
    """[VERIFY] RAGFlow-HTTP-API: Upload -> Parse. Pfade je RAGFlow-Version pruefen.
       Upload: POST /api/v1/datasets/{ds}/documents (multipart 'file')
       Parse : POST /api/v1/datasets/{ds}/chunks   {"document_ids":[...]}"""
    if not (RAGFLOW_API_URL and RAGFLOW_API_KEY):
        return {"ok": False, "error": "RAGFlow nicht konfiguriert (URL/KEY fehlt)"}
    if not RAGFLOW_INGEST_DATASET_ID:
        return {"ok": False, "error": "RAGFLOW_INGEST_DATASET_ID fehlt -> kein Ziel-Dataset"}

    headers = {"Authorization": f"Bearer {RAGFLOW_API_KEY}"}
    base = f"{RAGFLOW_API_URL}/api/v1/datasets/{RAGFLOW_INGEST_DATASET_ID}"
    files = {"file": (filename, data, content_type or "application/octet-stream")}

    up = await http.post(f"{base}/documents", headers=headers, files=files, timeout=120.0)
    up.raise_for_status()
    body = up.json()
    docs = body.get("data") or body.get("documents") or []
    doc_ids = [d.get("id") for d in docs if isinstance(d, dict) and d.get("id")]

    parse_status = "uebersprungen (keine doc_id erkannt)"
    if doc_ids:
        try:
            pr = await http.post(f"{base}/chunks", headers=headers,
                                 json={"document_ids": doc_ids}, timeout=60.0)
            pr.raise_for_status()
            parse_status = "Parsing angestossen"
        except Exception as e:  # Upload hat geklappt; Parsing kann man in RAGFlow nachstossen
            parse_status = f"Parsing-Trigger fehlgeschlagen: {e}"

    return {"ok": True, "dataset_id": RAGFLOW_INGEST_DATASET_ID,
            "document_ids": doc_ids, "parse": parse_status}


async def _to_morphik(http: httpx.AsyncClient, filename: str, content_type: str, data: bytes,
                      metadata: dict | None) -> dict:
    """[VERIFY] Morphik-API: POST /ingest/file (multipart 'file' + 'metadata' JSON).
       Endpoint/Feldnamen gegen deine Morphik-Version pruefen."""
    if not MORPHIK_API_URL:
        return {"ok": False, "error": "Morphik nicht konfiguriert (MORPHIK_API_URL fehlt)"}
    headers = {"Authorization": f"Bearer {MORPHIK_API_KEY}"} if MORPHIK_API_KEY else {}
    files = {"file": (filename, data, content_type or "application/octet-stream")}
    form = {"metadata": json.dumps(metadata or {})}
    r = await http.post(f"{MORPHIK_API_URL}/ingest/file", headers=headers,
                        files=files, data=form, timeout=120.0)
    r.raise_for_status()
    try:
        body = r.json()
    except Exception:
        body = {"raw": r.text[:500]}
    doc_id = body.get("external_id") or body.get("document_id") or body.get("id")
    return {"ok": True, "document_id": doc_id, "response": body if doc_id is None else None}


# ===========================================================================
#  HTTP-API
# ===========================================================================
@app.get("/healthz")
async def healthz():
    return {"ok": True,
            "ragflow": bool(RAGFLOW_API_URL and RAGFLOW_API_KEY and RAGFLOW_INGEST_DATASET_ID),
            "morphik": bool(MORPHIK_API_URL),
            "spreadsheet_to_morphik": SPREADSHEET_TO_MORPHIK}


@app.post("/classify")
async def classify_only(file: UploadFile = File(...)):
    data = await file.read()
    target, reason = classify(file.filename or "datei", file.content_type or "", data)
    log.info("[classify] %s (%s, %dB) -> %s | %s",
             file.filename, file.content_type, len(data), target, reason)
    return {"filename": file.filename, "size": len(data),
            "target": target, "reason": reason}


@app.post("/ingest")
async def ingest(file: UploadFile = File(...), metadata: str = Form("{}")):
    data = await file.read()
    filename = file.filename or "datei"
    ct = file.content_type or ""
    target, reason = classify(filename, ct, data)
    # Morphik gewaehlt, aber nicht konfiguriert -> auf RAGFlow zurueckfallen.
    if target == MORPHIK and not MORPHIK_API_URL and INGEST_FALLBACK_TO_RAGFLOW:
        reason += " | Morphik aus -> RAGFlow-Fallback"
        target = RAGFLOW
    log.info("[ingest] %s (%s, %dB) -> %s | %s", filename, ct, len(data), target, reason)

    try:
        meta = json.loads(metadata) if metadata else {}
    except Exception:
        meta = {}
    meta.setdefault("source", "open-webui")
    meta.setdefault("filename", filename)

    async with httpx.AsyncClient() as http:
        try:
            if target == MORPHIK:
                result = await _to_morphik(http, filename, ct, data, meta)
            else:
                result = await _to_ragflow(http, filename, ct, data)
        except httpx.HTTPStatusError as e:
            code = e.response.status_code if e.response is not None else "?"
            body = e.response.text[:300] if e.response is not None else ""
            log.error("[ingest] %s HTTP %s: %s", target, code, body)
            return JSONResponse(status_code=502, content={
                "target": target, "reason": reason,
                "ok": False, "error": f"{target}: HTTP {code}", "detail": body})
        except Exception as e:
            log.exception("[ingest] %s fehlgeschlagen", target)
            return JSONResponse(status_code=502, content={
                "target": target, "reason": reason, "ok": False, "error": str(e)})

    status = 200 if result.get("ok") else 502
    return JSONResponse(status_code=status, content={
        "filename": filename, "size": len(data),
        "target": target, "reason": reason, **result})
