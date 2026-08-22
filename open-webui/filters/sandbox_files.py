"""
title: Sandbox Files (Downloads aus dem research-agent)
author: local-ai-stack
version: 0.1.0
required_open_webui_version: 0.5.0
description: >
  Macht Dateien, die der research-agent in der Code-Sandbox ERZEUGT hat, im Chat
  als klickbare Download-Chips verfuegbar (statt sie als riesigen JSON-/Textblock
  in die Antwort zu kippen).

  Wie es funktioniert: Der Agent haengt seine erzeugten Dateien als HTML-Kommentar
  an die Antwort (im Markdown unsichtbar):
      <!--OWUI_FILES [{"name":..., "content_type":..., "b64":...}] OWUI_FILES-->
  Dieser outlet-Filter schneidet den Block heraus, legt jede Datei als ECHTE
  OWUI-Datei ab (Storage + Files-Tabelle) und haengt sie an message["files"].
  Open WebUI rendert daraus die gewohnten Datei-Kacheln mit Download.

  Installation: OWUI -> Admin -> Functions -> "+" -> Code einfuegen -> aktivieren,
  global ODER dem Modell "research-agent" zuweisen. Keine Valve ist Pflicht.

  Ohne diesen Filter bleibt der Block unsichtbar (HTML-Kommentar) — es geht also
  nichts kaputt, es fehlen nur die Download-Chips.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import uuid

from pydantic import BaseModel, Field

log = logging.getLogger("owui.sandbox_files")

# Muss zu agent/common.py (FILES_MARK_BEGIN/END) passen.
_BLOCK_RE = re.compile(r"<!--OWUI_FILES\s*(.*?)\s*OWUI_FILES-->", re.DOTALL)


def _strip_block(text: str) -> str:
    return _BLOCK_RE.sub("", text or "").rstrip()


def _parse_block(text: str) -> list:
    """Alle Anhang-Bloecke der Nachricht einsammeln (robust gegen kaputtes JSON)."""
    items = []
    for m in _BLOCK_RE.finditer(text or ""):
        try:
            data = json.loads(m.group(1))
            if isinstance(data, list):
                items.extend(x for x in data if isinstance(x, dict))
        except Exception:
            log.warning("sandbox_files: Anhang-Block nicht parsbar - uebersprungen")
    return items


class Filter:
    class Valves(BaseModel):
        enabled: bool = Field(default=True, description="Filter aktiv")
        max_mb: int = Field(default=25, description="Maximalgroesse je Datei (MB)")
        keep_marker: bool = Field(
            default=False, description="Anhang-Block im Text stehen lassen (Debug)")

    def __init__(self):
        self.valves = self.Valves()

    def _save(self, user_id: str, name: str, ctype: str, raw: bytes):
        """Datei in OWUI ablegen -> (file_id, groesse) oder None.
        Nutzt die prozess-internen OWUI-Module (der Filter laeuft IM OWUI-Container),
        daher kein HTTP und kein API-Key noetig."""
        try:
            from open_webui.storage.provider import Storage
            from open_webui.models.files import Files, FileForm
        except Exception as e:
            log.warning("sandbox_files: OWUI-Module nicht verfuegbar: %s", e)
            return None

        fid = str(uuid.uuid4())
        safe = (name or "datei").replace("/", "_").replace("\\", "_")
        try:
            import io

            _contents, path = Storage.upload_file(io.BytesIO(raw), f"{fid}_{safe}", {})
        except Exception as e:
            log.exception("sandbox_files: Storage.upload_file fehlgeschlagen: %s", e)
            return None

        form = FileForm(
            id=fid,
            filename=safe,
            path=path,
            data={},
            meta={"name": safe, "content_type": ctype or "application/octet-stream",
                  "size": len(raw), "source": "research-agent (Code-Sandbox)"},
        )
        try:
            res = Files.insert_new_file(user_id, form)
            # In OWUI 0.11 ist insert_new_file async -> ggf. awaiten
            if hasattr(res, "__await__"):
                return ("__await__", res, fid, len(raw))
            return (fid, len(raw)) if res else None
        except Exception as e:
            log.exception("sandbox_files: Files.insert_new_file fehlgeschlagen: %s", e)
            return None

    async def _save_async(self, user_id: str, name: str, ctype: str, raw: bytes):
        r = self._save(user_id, name, ctype, raw)
        if r and r[0] == "__await__":
            _tag, coro, fid, size = r
            try:
                ok = await coro
            except Exception as e:
                log.exception("sandbox_files: insert_new_file (async) fehlgeschlagen: %s", e)
                return None
            return (fid, size) if ok else None
        return r

    async def outlet(self, body: dict, __user__: dict | None = None, **kwargs) -> dict:
        if not self.valves.enabled:
            return body
        try:
            msgs = body.get("messages") or []
            if not msgs:
                return body
            last = msgs[-1]
            if not isinstance(last, dict) or last.get("role") != "assistant":
                return body
            content = last.get("content") or ""
            items = _parse_block(content)
            if not items:
                return body

            user_id = (__user__ or {}).get("id") or body.get("user_id") or "owui"
            limit = max(1, int(self.valves.max_mb)) * 1024 * 1024
            attached = list(last.get("files") or [])

            for it in items:
                name = it.get("name") or "datei"
                ctype = it.get("content_type") or "application/octet-stream"
                try:
                    raw = base64.b64decode(it.get("b64") or "")
                except Exception:
                    log.warning("sandbox_files: '%s' nicht dekodierbar", name)
                    continue
                if not raw or len(raw) > limit:
                    log.warning("sandbox_files: '%s' leer oder > %d MB", name, self.valves.max_mb)
                    continue
                saved = await self._save_async(user_id, name, ctype, raw)
                if not saved:
                    continue
                fid, size = saved
                attached.append({
                    "type": "file",
                    "id": fid,
                    "url": fid,          # FileItem baut daraus /api/v1/files/<id>/content
                    "name": name,
                    "size": size,
                    "content_type": ctype,
                    "collection_name": None,
                })
                log.info("sandbox_files: '%s' (%d B) angehaengt", name, size)

            if attached:
                last["files"] = attached
            if not self.valves.keep_marker:
                last["content"] = _strip_block(content)
        except Exception:
            # Niemals den Chat blockieren.
            log.exception("sandbox_files: outlet uebersprungen (Fehler)")
        return body

    async def inlet(self, body: dict, **kwargs) -> dict:
        return body
