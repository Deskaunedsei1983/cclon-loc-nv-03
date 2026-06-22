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
    fulltext: str | None = None  # Volltext der angehaengten Datei (Volltext-Modus)


_model = OpenAIModel(C.LLM_MODEL, provider=OpenAIProvider(base_url=C.LLM_BASE_URL, api_key=C.LLM_API_KEY))
_agent = Agent(_model, deps_type=Deps, system_prompt=C.SYSTEM_PROMPT, retries=2)


@_agent.tool
async def retrieve_documents(ctx: RunContext[Deps], query: str) -> str:
    """Interne Wissensbasen (RAGFlow) durchsuchen und Belegstellen liefern."""
    return await C.t_retrieve_documents(ctx.deps.http, query)


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
        return await C.t_retrieve_multimodal(ctx.deps.http, query)


def _result_text(result) -> str:
    return getattr(result, "output", None) or getattr(result, "data", None) or str(result)


async def run_agent(messages: list[dict], user_id: str = "owui",
                    request_body: dict | None = None) -> str:
    query = C.extract_query(messages)
    parts = [C.mem_search(_memory, query, user_id)]
    fulltext = None
    doc = C.read_full_document(request_body or {})  # Volltext der angehaengten Datei
    if doc:
        name, fulltext = doc
        cands = C.proper_noun_candidates(fulltext, 80)
        cand_line = ", ".join(f"{w}:{c}" for w, c in cands) or "(keine erkannt)"
        parts.append(
            f"VOLLTEXT-MODUS: Die KOMPLETTE Datei '{name}' ({len(fulltext)} Zeichen) liegt "
            f"im Sandbox-Arbeitsverzeichnis als 'document.txt'. Fuer Aufgaben ueber das GANZE "
            f"Dokument (zaehlen, alle Vorkommen, Statistik) NICHT die RAG-Schnipsel nehmen, "
            f"sondern 'run_code' nutzen und open('document.txt', encoding='utf-8').read() "
            f"verarbeiten (Ergebnis drucken, bei Bedarf CSV speichern).\n"
            f"WICHTIG bei Namens-/Haeufigkeitsaufgaben: Rate KEINE Begriffsliste aus dem "
            f"Gedaechtnis (Schreibweisen weichen ab -> falsche 0-Treffer). Nimm die unten aus "
            f"dem Text extrahierten, EXAKT geschriebenen Kandidaten und waehle die gefragten "
            f"Personen/Orte daraus. Zaehle mit Wortgrenzen (re.findall(r'\\b'+re.escape(w)+r'\\b')).\n"
            f"Haeufigste grossgeschriebene Tokens (exakte Schreibweise, Token:Anzahl):\n{cand_line}\n"
            f"Vorschau (Anfang):\n{fulltext[:800]}\n")
    parts.append(f"Aktuelle Anfrage:\n{query}")
    prompt = "\n".join(p for p in parts if p)
    async with httpx.AsyncClient() as http:
        result = await _agent.run(prompt, deps=Deps(http=http, fulltext=fulltext))
    answer = _result_text(result)
    C.mem_add(_memory, query, answer, user_id)
    return answer
