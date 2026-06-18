"""
agent_pydantic.py — Variante 1: PydanticAI-Agent mit Whitelist-Tools.
Schlank, typsicher, das Modell entscheidet selbst, welche Tools es ruft.
Aktiv bei AGENT_IMPL=pydantic (Default).
"""

from __future__ import annotations

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
    """Python in der luftdichten Sandbox ausfuehren (Office-Files/Notebooks moeglich)."""
    return await C.t_run_code(ctx.deps.http, code)


if C.MORPHIK_API_URL:
    @_agent.tool
    async def retrieve_multimodal(ctx: RunContext[Deps], query: str) -> str:
        """Morphik: bild-/tabellenlastige Dokumente (multimodal) durchsuchen."""
        return await C.t_retrieve_multimodal(ctx.deps.http, query)


def _result_text(result) -> str:
    return getattr(result, "output", None) or getattr(result, "data", None) or str(result)


async def run_agent(messages: list[dict], user_id: str = "owui") -> str:
    query = C.extract_query(messages)
    prompt = C.mem_search(_memory, query, user_id) + f"Aktuelle Anfrage:\n{query}"
    async with httpx.AsyncClient() as http:
        result = await _agent.run(prompt, deps=Deps(http=http))
    answer = _result_text(result)
    C.mem_add(_memory, query, answer, user_id)
    return answer
