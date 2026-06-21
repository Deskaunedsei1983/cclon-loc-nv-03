"""
title: Ingest Router (RAGFlow/Morphik Auto-Weiche)
author: local-ai-stack
version: 0.3.0
required_open_webui_version: 0.5.0
description: >
  Schiebt im Chat HOCHGELADENE Dateien automatisch in die richtige Wissensbasis:
  der 'ingest-router'-Dienst klassifiziert (Bild-/Scan-/Tabellen-lastig -> Morphik,
  sonst -> RAGFlow) und ingestiert. Danach findet der research-agent die Inhalte
  per RAG. Bricht NIE den Chat ab (Fehler werden nur geloggt/als Status gezeigt).

  KEY-LOS: braucht KEINEN OWUI-API-Key. Datei-Bytes werden mehrstufig beschafft:
    1) Bilder als data:-Base64 direkt aus der Nachricht (kein Fetch),
    2) LOKALER Pfad aus dem Datei-Deskriptor (Filter laeuft im OWUI-Container),
    3) GET /api/v1/files/{id}/content (in OWUI 0.9.5 nur MIT Auth -> oft 401),
    4) prozess-intern via open_webui Files/Storage,
    5) Fallback: der bereits extrahierte Text als .txt.

  Installation: OWUI -> Admin -> Functions -> "+" -> Code einfuegen -> aktivieren,
  global ODER dem Modell "research-agent" zuweisen. In den Valves ist nichts
  Pflicht; 'owui_base_url' nur anpassen, falls OWUI nicht auf :8080 lauscht.

  [VERIFY] Form von body["files"]/messages und /api/v1/files/{id}/content sind
  OWUI-versionsabhaengig (hier: 0.9.5-Annahmen). Bei Bedarf anpassen.
"""

from __future__ import annotations

import base64
import hashlib
import logging

from pydantic import BaseModel, Field

log = logging.getLogger("owui.ingest_router")

# data:<mime>;base64,<payload>
_MIME_EXT = {
    "image/png": "png", "image/jpeg": "jpg", "image/jpg": "jpg", "image/webp": "webp",
    "image/gif": "gif", "image/tiff": "tiff", "image/bmp": "bmp", "image/heic": "heic",
    "application/pdf": "pdf",
}


def _decode_data_url(url: str):
    """'data:image/png;base64,AAAA' -> (content_type, bytes) | (None, None)."""
    try:
        if not isinstance(url, str) or not url.startswith("data:") or "," not in url:
            return None, None
        header, payload = url.split(",", 1)
        meta = header[5:]  # nach 'data:'
        ctype = meta.split(";", 1)[0] or "application/octet-stream"
        if "base64" not in meta:
            return None, None
        return ctype, base64.b64decode(payload)
    except Exception:
        return None, None


def _ext_for(ctype: str) -> str:
    return _MIME_EXT.get((ctype or "").lower(), "bin")


def _collect(body: dict) -> list[dict]:
    """Alle ingestierbaren Anhaenge einsammeln. Jedes Item:
       {dedup, name, content_type, data?:bytes, fid?:str, text?:str}."""
    items: list[dict] = []

    # 1) Bilder als data:-URL direkt aus den Nachrichteninhalten (multimodal).
    for msg in body.get("messages") or []:
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict) or part.get("type") != "image_url":
                continue
            url = (part.get("image_url") or {}).get("url", "")
            ctype, data = _decode_data_url(url)
            if data:
                h = hashlib.sha1(data).hexdigest()
                items.append({"dedup": f"img:{h}", "name": f"upload_{h[:8]}.{_ext_for(ctype)}",
                              "content_type": ctype, "data": data})

    # 2) Datei-Deskriptoren (mehrere OWUI-Formen).
    cand = list(body.get("files") or [])
    cand += (body.get("metadata") or {}).get("files") or []
    for msg in body.get("messages") or []:
        if isinstance(msg, dict):
            cand += msg.get("files") or []

    for f in cand:
        if not isinstance(f, dict):
            continue
        inner = f.get("file") if isinstance(f.get("file"), dict) else f
        fid = inner.get("id") or f.get("id")
        meta = inner.get("meta") or {}
        name = inner.get("filename") or inner.get("name") or meta.get("name") or (fid or "datei")
        ctype = meta.get("content_type") or inner.get("content_type") or ""

        # 2a) Inline-Base64 im Deskriptor (manche Versionen).
        for maybe in (inner.get("url"), f.get("url"), inner.get("data")):
            if isinstance(maybe, str) and maybe.startswith("data:"):
                ct, data = _decode_data_url(maybe)
                if data:
                    h = hashlib.sha1(data).hexdigest()
                    items.append({"dedup": f"sha:{h}", "name": name,
                                  "content_type": ct or ctype, "data": data})
                    break
        else:
            # 2b) Extrahierter Text als allerletzter Fallback.
            data_field = inner.get("data")
            text = data_field.get("content") if isinstance(data_field, dict) else None
            if fid:
                items.append({"dedup": f"id:{fid}", "name": name,
                              "content_type": ctype, "fid": fid, "text": text,
                              "path": inner.get("path") or f.get("path")})
            elif text:
                h = hashlib.sha1(text.encode("utf-8", "ignore")).hexdigest()
                items.append({"dedup": f"txt:{h}", "name": name, "content_type": ctype,
                              "text": text})
    return items


def _read_internal(fid: str):
    """Prozess-interner Lesepfad ueber OWUIs eigene Module (kein HTTP, kein Key)."""
    try:
        from open_webui.models.files import Files

        rec = Files.get_file_by_id(fid)
        if not rec:
            return None
        path = getattr(rec, "path", None) or (getattr(rec, "meta", None) or {}).get("path")
        if not path:
            return None
        try:
            from open_webui.storage.provider import Storage

            local = Storage.get_file(path)
        except Exception:
            local = path  # aeltere Versionen: path ist schon lokal
        with open(local, "rb") as fh:
            return fh.read()
    except Exception:
        return None


class Filter:
    class Valves(BaseModel):
        enabled: bool = Field(default=True, description="Filter aktiv")
        ingest_router_url: str = Field(
            default="http://ingest-router:8000",
            description="Basis-URL des ingest-router-Dienstes")
        owui_base_url: str = Field(
            default="http://localhost:8080",
            description="OWUI-Basis-URL (intern) fuer den key-losen Datei-GET")
        emit_status: bool = Field(
            default=True, description="Fortschritt als Status im Chat anzeigen")

    def __init__(self):
        self.valves = self.Valves()
        self._seen: set[str] = set()  # bereits ingestierte Dateien (pro Prozess)

    async def _emit(self, emitter, text: str, done: bool = False):
        if emitter and self.valves.emit_status:
            try:
                await emitter({"type": "status", "data": {"description": text, "done": done}})
            except Exception:
                pass

    async def _fetch_http(self, session, fid: str):
        """GET /api/v1/files/{id}/content OHNE Auth (WEBUI_AUTH=false)."""
        url = f"{self.valves.owui_base_url.rstrip('/')}/api/v1/files/{fid}/content"
        try:
            async with session.get(url) as r:
                if r.status == 200:
                    return await r.read()
                log.warning("ingest_router: GET content %s -> HTTP %s", fid, r.status)
        except Exception:
            log.warning("ingest_router: GET content %s fehlgeschlagen", fid)
        return None

    async def _resolve_bytes(self, session, item: dict):
        """Bytes + content_type + name beschaffen; mehrstufig, key-los."""
        if item.get("data") is not None:
            return item["data"], item.get("content_type") or "application/octet-stream", item["name"]
        # 0) Lokale Datei direkt lesen (Filter laeuft IM OWUI-Container -> kein HTTP/Key).
        #    OWUI 0.9.5 liefert /api/v1/files/{id}/content nur MIT Auth (401) -> Pfad nutzen.
        p = item.get("path")
        if p:
            try:
                with open(p, "rb") as fh:
                    return fh.read(), item.get("content_type") or "application/octet-stream", item["name"]
            except Exception:
                log.warning("ingest_router: lokaler Pfad nicht lesbar (%s)", p)
        if item.get("fid"):
            data = await self._fetch_http(session, item["fid"]) or _read_internal(item["fid"])
            if data is not None:
                return data, item.get("content_type") or "application/octet-stream", item["name"]
        if item.get("text"):
            name = item["name"] if item["name"].lower().endswith(".txt") else item["name"] + ".txt"
            return item["text"].encode("utf-8", "ignore"), "text/plain", name
        return None, None, None

    async def _ingest_one(self, session, item: dict, emitter) -> None:
        import aiohttp

        data, ctype, name = await self._resolve_bytes(session, item)
        if data is None:
            await self._emit(emitter, f"'{item['name']}': keine Datei-Bytes erreichbar "
                                      f"(owui_base_url pruefen)", done=True)
            return
        await self._emit(emitter, f"Klassifiziere & importiere '{name}' …")
        form = aiohttp.FormData()
        form.add_field("file", data, filename=name,
                       content_type=ctype or "application/octet-stream")
        url = f"{self.valves.ingest_router_url.rstrip('/')}/ingest"
        async with session.post(url, data=form) as r:
            body = await r.json(content_type=None)
        if r.status == 200 and body.get("ok"):
            await self._emit(emitter, f"'{name}' -> {body.get('target')} ({body.get('reason','')})",
                             done=True)
            log.info("ingest_router: %s -> %s | %s", name, body.get("target"), body.get("reason"))
        else:
            await self._emit(emitter, f"'{name}': Import-Fehler ({body.get('error','?')})", done=True)
            log.error("ingest_router: %s -> Fehler %s", name, body)

    async def inlet(self, body: dict, __event_emitter__=None, **kwargs) -> dict:
        if not self.valves.enabled:
            return body
        try:
            items = [it for it in _collect(body) if it["dedup"] not in self._seen]
            if not items:
                return body
            import aiohttp

            timeout = aiohttp.ClientTimeout(total=180)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                for it in items:
                    self._seen.add(it["dedup"])  # vor dem Call markieren -> kein Doppel-Ingest
                    try:
                        await self._ingest_one(session, it, __event_emitter__)
                    except Exception:
                        log.exception("ingest_router: Ingest von %s fehlgeschlagen", it.get("name"))
        except Exception:
            # Niemals den Chat blockieren, egal was schiefgeht.
            log.exception("ingest_router: inlet uebersprungen (Fehler)")
        return body

    async def outlet(self, body: dict, **kwargs) -> dict:
        return body
