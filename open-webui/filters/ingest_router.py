"""
title: Ingest Router (RAGFlow/Morphik Auto-Weiche)
author: local-ai-stack
version: 0.1.0
required_open_webui_version: 0.5.0
description: >
  Schiebt im Chat HOCHGELADENE Dateien automatisch in die richtige Wissensbasis:
  der 'ingest-router'-Dienst klassifiziert (Bild-/Scan-/Tabellen-lastig -> Morphik,
  sonst -> RAGFlow) und ingestiert. Danach findet der research-agent die Inhalte
  per RAG. Bricht NIE den Chat ab (Fehler werden nur geloggt/als Status gezeigt).

  Installation: OWUI -> Admin -> Functions -> "+" -> Code einfuegen -> aktivieren,
  global ODER dem Modell "research-agent" zuweisen. Dann in den Valves die
  owui_api_key setzen (Settings -> Account -> API Keys), damit der Filter die
  Datei-Bytes aus OWUI laden darf.

  [VERIFY] Die Form von body["files"]/["metadata"]["files"] und der Datei-Download
  (/api/v1/files/{id}/content) sind OWUI-versionsabhaengig -> bei Bedarf anpassen.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field

log = logging.getLogger("owui.ingest_router")


def _iter_files(body: dict):
    """Alle Datei-Eintraege aus dem Request einsammeln (mehrere OWUI-Formen)."""
    seen_local = []
    cand = []
    cand += body.get("files") or []
    cand += (body.get("metadata") or {}).get("files") or []
    for msg in body.get("messages") or []:
        if isinstance(msg, dict):
            cand += msg.get("files") or []
    for f in cand:
        if not isinstance(f, dict):
            continue
        inner = f.get("file") if isinstance(f.get("file"), dict) else f
        fid = inner.get("id") or f.get("id")
        if not fid:
            continue
        meta = inner.get("meta") or {}
        name = inner.get("filename") or inner.get("name") or meta.get("name") or f"{fid}"
        ctype = meta.get("content_type") or inner.get("content_type") or ""
        seen_local.append({"id": fid, "name": name, "content_type": ctype})
    return seen_local


class Filter:
    class Valves(BaseModel):
        enabled: bool = Field(default=True, description="Filter aktiv")
        ingest_router_url: str = Field(
            default="http://ingest-router:8000",
            description="Basis-URL des ingest-router-Dienstes")
        owui_base_url: str = Field(
            default="http://localhost:8080",
            description="OWUI-Basis-URL (intern), um Datei-Bytes zu laden")
        owui_api_key: str = Field(
            default="",
            description="OWUI API-Key (Account -> API Keys) fuer den Datei-Download")
        emit_status: bool = Field(
            default=True, description="Fortschritt als Status im Chat anzeigen")

    def __init__(self):
        self.valves = self.Valves()
        self._seen: set[str] = set()  # bereits ingestierte File-IDs (pro Prozess)

    async def _emit(self, emitter, text: str, done: bool = False):
        if emitter and self.valves.emit_status:
            try:
                await emitter({"type": "status",
                               "data": {"description": text, "done": done}})
            except Exception:
                pass

    async def _download(self, session, fid: str) -> bytes | None:
        url = f"{self.valves.owui_base_url.rstrip('/')}/api/v1/files/{fid}/content"
        headers = {"Authorization": f"Bearer {self.valves.owui_api_key}"} \
            if self.valves.owui_api_key else {}
        async with session.get(url, headers=headers) as r:
            if r.status != 200:
                log.warning("ingest_router: Datei %s laden -> HTTP %s", fid, r.status)
                return None
            return await r.read()

    async def _ingest_one(self, session, f: dict, emitter) -> None:
        import aiohttp

        fid = f["id"]
        await self._emit(emitter, f"Klassifiziere & importiere '{f['name']}' …")
        data = await self._download(session, fid)
        if data is None:
            await self._emit(emitter, f"'{f['name']}': Download fehlgeschlagen "
                                      f"(owui_api_key/owui_base_url pruefen)", done=True)
            return
        form = aiohttp.FormData()
        form.add_field("file", data, filename=f["name"],
                       content_type=f.get("content_type") or "application/octet-stream")
        url = f"{self.valves.ingest_router_url.rstrip('/')}/ingest"
        async with session.post(url, data=form) as r:
            body = await r.json(content_type=None)
        if r.status == 200 and body.get("ok"):
            tgt, reason = body.get("target"), body.get("reason", "")
            await self._emit(emitter, f"'{f['name']}' -> {tgt} ({reason})", done=True)
            log.info("ingest_router: %s -> %s | %s", f["name"], tgt, reason)
        else:
            await self._emit(emitter, f"'{f['name']}': Import-Fehler "
                                      f"({body.get('error','?')})", done=True)
            log.error("ingest_router: %s -> Fehler %s", f["name"], body)

    async def inlet(self, body: dict, __event_emitter__=None, **kwargs) -> dict:
        if not self.valves.enabled:
            return body
        try:
            files = [f for f in _iter_files(body) if f["id"] not in self._seen]
            if not files:
                return body
            import aiohttp

            timeout = aiohttp.ClientTimeout(total=180)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                for f in files:
                    self._seen.add(f["id"])  # vor dem Call markieren -> kein Doppel-Ingest
                    try:
                        await self._ingest_one(session, f, __event_emitter__)
                    except Exception:
                        log.exception("ingest_router: Ingest von %s fehlgeschlagen", f["name"])
        except Exception:
            # Niemals den Chat blockieren, egal was schiefgeht.
            log.exception("ingest_router: inlet uebersprungen (Fehler)")
        return body

    async def outlet(self, body: dict, **kwargs) -> dict:
        return body
