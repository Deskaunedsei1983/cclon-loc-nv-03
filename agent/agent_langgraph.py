"""
agent_langgraph.py — Variante 2: echter iterativer Critic-Loop.
   gather (RAGFlow/Morphik) -> draft (LLM) -> execute (Code in Sandbox)
   -> critic (LLM bewertet Belegtreue/Vollstaendigkeit) -> ggf. zurueck zu draft.
Aktiv bei AGENT_IMPL=langgraph.
"""

from __future__ import annotations

import re
from typing import TypedDict

import httpx
from langchain_openai import ChatOpenAI
from langgraph.graph import StateGraph, START, END

import common as C

_memory = C.build_memory()
_llm = ChatOpenAI(model=C.LLM_MODEL, base_url=C.LLM_BASE_URL, api_key=C.LLM_API_KEY, temperature=0.2)

CODE_RE = re.compile(r"```python\s+(.*?)```", re.DOTALL)
MAX_ITER = 3


class State(TypedDict, total=False):
    query: str
    mem_context: str
    retrieved: str
    draft: str
    exec_out: str
    critique: str
    approved: bool
    iteration: int


async def gather(state: State) -> State:
    async with httpx.AsyncClient() as http:
        docs = await C.t_retrieve_documents(http, state["query"])
        extra = ""
        if C.MORPHIK_API_URL:
            extra = "\n\n[Multimodal]\n" + await C.t_retrieve_multimodal(http, state["query"])
    return {"retrieved": (docs + extra)[:6000], "iteration": 0}


async def draft(state: State) -> State:
    sys = C.SYSTEM_PROMPT
    parts = [
        state.get("mem_context", ""),
        f"Frage:\n{state['query']}",
        f"\nBelege (RAGFlow/Morphik):\n{state.get('retrieved','(keine)')}",
    ]
    if state.get("critique"):
        parts.append(f"\nVerbesser den vorigen Entwurf gemaess Kritik:\n{state['critique']}")
        parts.append(f"\nVoriger Entwurf:\n{state.get('draft','')}")
    parts.append("\nBrauchst du eine Berechnung/Datei, gib EINEN ```python ...``` Block aus; "
                 "er wird in der Sandbox ausgefuehrt.")
    resp = await _llm.ainvoke([{"role": "system", "content": sys},
                               {"role": "user", "content": "\n".join(parts)}])
    return {"draft_node": resp.content, "iteration": state.get("iteration", 0) + 1}


async def execute(state: State) -> State:
    blocks = CODE_RE.findall(state.get("draft_node", ""))
    if not blocks:
        return {"exec_out": ""}
    async with httpx.AsyncClient() as http:
        outs = [await C.t_run_code(http, b) for b in blocks[:2]]
    return {"exec_out": "\n\n".join(outs)[:6000]}


async def critic(state: State) -> State:
    judge = (
        "Bewerte den Entwurf streng. Ist er durch die Belege gedeckt, vollstaendig, "
        "und (falls Code) durch das Ausfuehrungsergebnis bestaetigt? "
        "Antworte in Zeile 1 mit GENAU 'APPROVE' oder 'REVISE', danach eine kurze Begruendung."
    )
    ctx = (f"Frage:\n{state['query']}\n\nBelege:\n{state.get('retrieved','')}\n\n"
           f"Entwurf:\n{state.get('draft','')}\n\nAusfuehrungsergebnis:\n{state.get('exec_out','(keins)')}")
    resp = await _llm.ainvoke([{"role": "system", "content": judge},
                               {"role": "user", "content": ctx}])
    text = resp.content.strip()
    approved = text.upper().startswith("APPROVE") or state.get("iteration", 0) >= MAX_ITER
    return {"approved": approved, "critique": text}


def route(state: State) -> str:
    return "done" if state.get("approved") else "revise"


def _build():
    b = StateGraph(State)
    b.add_node("gather", gather)
    b.add_node("draft_node", draft)
    b.add_node("execute", execute)
    b.add_node("critic", critic)
    b.add_edge(START, "gather")
    b.add_edge("gather", "draft_node")
    b.add_edge("draft_node", "execute")
    b.add_edge("execute", "critic")
    b.add_conditional_edges("critic", route, {"revise": "draft_node", "done": END})
    return b.compile()


_graph = _build()


async def run_agent(messages: list[dict], user_id: str = "owui") -> str:
    query = C.extract_query(messages)
    mem_context = C.mem_search(_memory, query, user_id)
    final = await _graph.ainvoke({"query": query, "mem_context": mem_context})
    answer = final.get("draft_node", "")
    if final.get("exec_out"):
        answer += f"\n\n---\nAusfuehrungsergebnis:\n{final['exec_out']}"
    C.mem_add(_memory, query, answer, user_id)
    return answer
