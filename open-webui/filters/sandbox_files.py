"""
title: Sandbox Files (Downloads aus dem research-agent)
author: local-ai-stack
version: 0.1.0
required_open_webui_version: 0.5.0
description: >
  Macht Dateien, die der research-agent in der Code-Sandbox ERZEUGT hat, im Chat
  als klickbare Download-Chips verfuegbar (statt sie als riesigen JSON-/Textblock
  in die Antwort zu kippen).

  Wie es funktioniert: Der Agent haengt seine erzeugten Dateien als Marker-Block
  an die Antwort:
      <!--OWUI_FILES [{"name":..., "content_type":..., "path":...}] OWUI_FILES-->
  Dieser outlet-Filter schneidet den Block heraus, legt jede Datei als ECHTE
  OWUI-Datei ab (Storage + Files-Tabelle) und
    a) haengt sie an message["files"]  -> Datei-Kachel, und
    b) ersetzt den Block durch echte Markdown-DOWNLOAD-LINKS.
  (b) ist wichtig: Ein Klick auf die KACHEL oeffnet in OWUI nur das Vorschau-Modal
  (FileItem.svelte: bei type=='file' immer showModal) — der Download steckt dort
  versteckt hinter dem Dateinamen. Der Markdown-Link ist EIN Klick.

  HINWEIS: Der Marker ist KEIN unsichtbarer Kommentar — OWUI sanitized HTML und
  zeigt ihn als Text. Ohne aktiven Filter bleibt er also sichtbar (kurz, da nur
  Pfade, kein base64). Die gefilterte Fassung erscheint nach dem Neuladen des
  Chats, weil die gestreamte Antwort im Browser schon steht.

  GROSSE Dateien (> SANDBOX_INLINE_MAX im Agent, Default 20 MB) werden NICHT
  base64 durch die Antwort geschleust: die Sandbox schreibt sie in ihr Volume, der
  Agent meldet nur den Pfad, und dieser Filter liest sie direkt aus dem read-only
  gemounteten /sandbox-work (Pfad wird gegen den Mountpoint geprueft).

  Installation: OWUI -> Admin -> Functions -> "+" -> Code einfuegen -> aktivieren,
  global ODER dem Modell "research-agent" zuweisen. Keine Valve ist Pflicht.

  Ohne diesen Filter bleibt der Block unsichtbar (HTML-Kommentar) — es geht also
  nichts kaputt, es fehlen nur die Download-Chips.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import uuid

from pydantic import BaseModel, Field

log = logging.getLogger("owui.sandbox_files")

# Muss zu agent/common.py (FILES_MARK_BEGIN/END) passen.
_BLOCK_RE = re.compile(r"<!--OWUI_FILES\s*(.*?)\s*OWUI_FILES-->", re.DOTALL)


# Die vom Agent geschriebene Klartext-Zeile (wird durch echte Links ersetzt).
_HUMAN_RE = re.compile(r"\n*\*\*Erzeugte Dateien:\*\*[^\n]*", re.IGNORECASE)


def _hr(n) -> str:
    n = int(n or 0)
    if n >= 1048576:
        return f"{n/1048576:.1f} MB"
    return f"{n/1024:.1f} KB" if n >= 1024 else f"{n} B"


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
        max_mb: int = Field(default=200, description="Maximalgroesse je Datei (MB)")
        sandbox_mount: str = Field(
            default="/sandbox-work",
            description="Mountpoint des Sandbox-Volumes in OWUI (read-only). "
                        "Grosse Dateien werden von hier gelesen statt base64 transportiert.")
        sandbox_path: str = Field(
            default="/home/sandbox/work",
            description="Derselbe Ordner AUS SICHT der Sandbox. Wird gebraucht, um "
                        "den Datei-Browser (rechte Seitenleiste) auf die neue Datei "
                        "zu schicken — der spricht Sandbox-Pfade.")
        open_file_nav: bool = Field(
            default=True,
            description="Nach der Antwort den Datei-Browser oeffnen und die zuletzt "
                        "erzeugte Datei anzeigen (Event 'terminal:display_file').")
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

    async def outlet(self, body: dict, __user__: dict | None = None,
                     __event_emitter__=None, **kwargs) -> dict:
        if not self.valves.enabled:
            return body
        last_sandbox_path = None
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
            links: list = []

            for it in items:
                name = it.get("name") or "datei"
                ctype = it.get("content_type") or "application/octet-stream"
                raw = b""
                cur_sandbox_path = None
                if it.get("b64"):
                    # Kleine Datei: kam base64 durch die Chat-Antwort.
                    try:
                        raw = base64.b64decode(it["b64"])
                    except Exception:
                        log.warning("sandbox_files: '%s' nicht dekodierbar", name)
                        continue
                elif it.get("path"):
                    # GROSSE Datei: liegt im read-only gemounteten Sandbox-Volume.
                    # Pfad haerten — nur unterhalb des Mountpoints lesen.
                    p = os.path.realpath(str(it["path"]))
                    root = os.path.realpath(self.valves.sandbox_mount)
                    if not (p == root or p.startswith(root + os.sep)):
                        log.warning("sandbox_files: Pfad ausserhalb %s abgelehnt: %s", root, p)
                        continue
                    # Denselben Pfad aus Sicht der SANDBOX merken — der
                    # Datei-Browser spricht Sandbox-Pfade, nicht Mount-Pfade.
                    cur_sandbox_path = self.valves.sandbox_path.rstrip("/") + p[len(root):]
                    try:
                        if os.path.getsize(p) > limit:
                            log.warning("sandbox_files: '%s' > %d MB -> uebersprungen",
                                        name, self.valves.max_mb)
                            continue
                        with open(p, "rb") as fh:
                            raw = fh.read()
                    except Exception as e:
                        log.warning("sandbox_files: '%s' nicht lesbar (%s): %s", name, p, e)
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
                links.append(f"[{name} ({_hr(size)})](/api/v1/files/{fid}/content)")
                if cur_sandbox_path:
                    last_sandbox_path = cur_sandbox_path
                log.info("sandbox_files: '%s' (%d B) angehaengt", name, size)

            if attached:
                last["files"] = attached
            if not self.valves.keep_marker:
                # Marker raus UND durch echte Markdown-DOWNLOAD-LINKS ersetzen.
                # Wichtig: Ein Klick auf die Datei-KACHEL oeffnet in OWUI nur das
                # Vorschau-Modal (FileItem.svelte: bei type=='file' immer showModal);
                # der Download steckt dort versteckt hinter dem Dateinamen. Ein
                # direkter Link ist EIN Klick — und OWUI akzeptiert das Auth-Token
                # auch aus dem Cookie, der Browser-Download funktioniert also.
                body_text = _strip_block(content)
                if links:
                    body_text = _HUMAN_RE.sub("", body_text).rstrip()
                    body_text += "\n\n**Erzeugte Dateien:** " + " · ".join(links)
                last["content"] = body_text
        except Exception:
            # Niemals den Chat blockieren.
            log.exception("sandbox_files: outlet uebersprungen (Fehler)")

        # Datei-Browser (rechte Seitenleiste) auf die zuletzt erzeugte Datei
        # schicken. OWUI 0.11.0: Chat.svelte -> terminalEventHandler ->
        # 'terminal:display_file' -> displayFileHandler -> showControls=true +
        # showFileNavPath -> ChatControls schaltet auf den Reiter "Files" und
        # FileNav oeffnet den Ordner samt Vorschau. Braucht einen im Modell
        # verknuepften Terminal-Server; ohne den passiert schlicht nichts.
        if self.valves.open_file_nav and last_sandbox_path and __event_emitter__:
            try:
                await __event_emitter__({
                    "type": "terminal:display_file",
                    "data": {"path": last_sandbox_path},
                })
            except Exception as e:
                log.debug("sandbox_files: display_file-Event nicht zustellbar: %s", e)
        return body

    async def inlet(self, body: dict, **kwargs) -> dict:
        return body
