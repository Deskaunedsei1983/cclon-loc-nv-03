"""
agent_pydantic.py — Variante 1: PydanticAI-Agent mit Whitelist-Tools.
Schlank, typsicher, das Modell entscheidet selbst, welche Tools es ruft.
Aktiv bei AGENT_IMPL=pydantic (Default).
"""

from __future__ import annotations

import base64
from dataclasses import dataclass

import httpx

from pydantic_ai import Agent, RunContext
# pydantic-ai 1.x hat OpenAIModel -> OpenAIChatModel umbenannt (alter Name bleibt
# eine Weile als Alias). Beide Faelle abfangen, damit der Tool-Calling-Pfad ueber
# 1.x-Versionen hinweg zuverlaessig laedt (sonst stiller Fallback auf LangGraph).
try:
    from pydantic_ai.models.openai import OpenAIModel
except ImportError:  # neuere 1.x
    from pydantic_ai.models.openai import OpenAIChatModel as OpenAIModel
from pydantic_ai.providers.openai import OpenAIProvider

import common as C

_memory = C.build_memory()


@dataclass
class Deps:
    http: httpx.AsyncClient
    fulltext: str | None = None   # Volltext der angehaengten Datei (Volltext-Modus)
    only_doc: str | None = None   # Retrieval auf die angehaengte Datei eingrenzen


_model = OpenAIModel(C.LLM_MODEL, provider=OpenAIProvider(base_url=C.LLM_BASE_URL, api_key=C.LLM_API_KEY))
_agent = Agent(_model, deps_type=Deps, system_prompt=C.SYSTEM_PROMPT, retries=2)


@_agent.tool
async def retrieve_documents(ctx: RunContext[Deps], query: str) -> str:
    """Interne Wissensbasen (RAGFlow) durchsuchen und Belegstellen liefern."""
    return await C.t_retrieve_documents(ctx.deps.http, query, only_doc=ctx.deps.only_doc)


@_agent.tool
async def search_web(ctx: RunContext[Deps], query: str) -> str:
    """Websuche ueber den PII-Masking-Proxy."""
    return await C.t_search_web(ctx.deps.http, query)


@_agent.tool
async def run_code(ctx: RunContext[Deps], code: str) -> str:
    """Python in der luftdichten Sandbox ausfuehren (Office-Files/Notebooks moeglich).
    Im Volltext-Modus liegt die ganze Datei als 'document.txt' im Arbeitsverzeichnis."""
    files = None
    if ctx.deps.fulltext:
        files = {"document.txt": base64.b64encode(
            ctx.deps.fulltext.encode("utf-8")).decode("ascii")}
    return await C.t_run_code(ctx.deps.http, code, files=files)


if C.MORPHIK_API_URL:
    @_agent.tool
    async def retrieve_multimodal(ctx: RunContext[Deps], query: str) -> str:
        """Morphik: bild-/tabellenlastige Dokumente (multimodal) durchsuchen."""
        return await C.t_retrieve_multimodal(ctx.deps.http, query, only_doc=ctx.deps.only_doc)


def _result_text(result) -> str:
    return getattr(result, "output", None) or getattr(result, "data", None) or str(result)


async def run_agent(messages: list[dict], user_id: str = "owui",
                    request_body: dict | None = None) -> str:
    query = C.extract_query(messages)
    C.schedule_ingest(request_body or {})  # Chat-Upload lokal nach RAGFlow/Morphik (nicht-blockierend)
    parts = [C.mem_search(_memory, query, user_id)]
    fulltext = only_doc = None
    doc = C.read_full_document(request_body or {})  # Volltext der angehaengten Datei
    if doc:
        name, fulltext = doc
        only_doc = name
        cands = C.proper_noun_candidates(fulltext, 150)
        cand_line = ", ".join(f"{w}:{c}" for w, c in cands) or "(keine erkannt)"
        parts.append(
            f"VOLLTEXT-MODUS: Die KOMPLETTE Datei '{name}' ({len(fulltext)} Zeichen) liegt "
            f"im Sandbox-Arbeitsverzeichnis als 'document.txt'. Aufgaben ueber das GANZE Dokument "
            f"NUR damit loesen (NICHT mit RAG-Schnipseln) via 'run_code'.\n"
            f"Bei Namens-/Haeufigkeitsaufgaben: (1) Rate KEINE Begriffe (falsche Schreibweisen -> "
            f"0-Treffer) — waehle Personen/Orte aus den unten EXAKT extrahierten Tokens. (2) NUR "
            f"echte Eigennamen; KEINE Himmelsrichtungen (Norden/Osten/...), Voelker-/Gattungs-"
            f"begriffe (Hobbits, Halblinge, Elben) oder Allgemeinwoerter; Mehrwortnamen ('Minas "
            f"Tirith') ganz zaehlen, nicht das Fragment. (3) JEDEN Namen EXAKT per run_code gegen "
            f"document.txt zaehlen (re.findall(r'\\b'+re.escape(name)+r'\\b', text)), absteigend, "
            f"CSV speichern. Vorschau-Zahlen sind unvollstaendig (nur Top-Tokens) — NIE '< 100' "
            f"schreiben, immer die exakte Zahl aus dem Code.\n"
            f"Extrahierte Tokens (exakte Schreibweise, Token:Anzahl):\n{cand_line}\n"
            f"Vorschau (Anfang):\n{fulltext[:600]}\n")
    parts.append(f"Aktuelle Anfrage:\n{query}")
    prompt = "\n".join(p for p in parts if p)
    async with httpx.AsyncClient() as http:
        result = await _agent.run(prompt, deps=Deps(http=http, fulltext=fulltext, only_doc=only_doc))
    answer = _result_text(result)
    C.mem_add(_memory, query, answer, user_id)
    return answer
