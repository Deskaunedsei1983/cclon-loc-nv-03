"""
agent_langgraph.py — Variante 2: iterativer Critic-Loop MIT deterministischer
Web-Gegenpruefung.
   gather (RAGFlow/Morphik)
     -> draft   (LLM, belegt; optional ```python)
     -> execute (Code in der Sandbox)
     -> verify  (DETERMINISTISCH: prueft strittige/zeitkritische Aussagen gegen das
                 Web ueber den PII-Masking-Proxy -> bestaetigt / aktueller / Widerspruch)
     -> critic  (LLM bewertet Belegtreue/Vollstaendigkeit UND ob die Web-Befunde
                 eingearbeitet sind) -> ggf. zurueck zu draft.
Aktiv bei AGENT_IMPL=langgraph. (Default ist pydantic; diese Variante bietet die
GARANTIERTE, nicht dem Modell-Ermessen ueberlassene Gegenpruefung.)
"""

from __future__ import annotations

import os
import re
from typing import TypedDict

import httpx
from langchain_openai import ChatOpenAI
from langgraph.graph import StateGraph, START, END

import common as C

_memory = C.build_memory()
_llm = ChatOpenAI(model=C.LLM_MODEL, base_url=C.LLM_BASE_URL, api_key=C.LLM_API_KEY, temperature=0.2)
# Deterministisch (temp 0) fuer die strukturierten Verify-Schritte:
_llm_strict = ChatOpenAI(model=C.LLM_MODEL, base_url=C.LLM_BASE_URL, api_key=C.LLM_API_KEY, temperature=0.0)

CODE_RE = re.compile(r"```python\s+(.*?)```", re.DOTALL)
_LIST_MARK = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s*")  # nur Listenmarker, KEINE Zahl-im-Text
MAX_ITER = int(os.environ.get("VERIFY_MAX_ITER", "3"))
VERIFY_MAX_QUERIES = int(os.environ.get("VERIFY_MAX_QUERIES", "3"))

# Diese Variante orchestriert Retrieval/Web/Code SELBST -> das Modell darf KEINE
# Tools aufrufen (sonst leakt z.B. gemma tool_call-Syntax in die Antwort). Eigener
# System-Prompt OHNE Tool-Aufruf-Framing (der geteilte C.SYSTEM_PROMPT ist
# tool-zentriert und nur fuer die pydantic-Variante gedacht).
_SYS_ORCHESTRATED = (
    "Du bist ein praeziser Engineering-Assistent fuer einen oesterreichischen "
    "Daten-Ingenieur (Sozialversicherung). Antworte auf Deutsch, knapp und konkret.\n"
    "WICHTIG: Rufe KEINE Tools/Funktionen auf und gib KEINE tool_call-/Funktions-Syntax "
    "aus. Die RAG-Belege sind unten bereits beigefuegt; eine Web-Gegenpruefung laeuft "
    "AUTOMATISCH nach deinem Entwurf (du musst nichts 'suchen'). Gruende die Antwort auf "
    "die beigefuegten Belege und die spaeter eingearbeiteten Web-Befunde, erfinde nichts. "
    "Brauchst du eine Berechnung/Datei, gib GENAU EINEN ```python ...``` Block aus (wird in "
    "der Sandbox ausgefuehrt). Nenne am Ende Quellen + erzeugte Dateinamen."
)

# Schutznetz: falls das Modell doch tool_call-Syntax emittiert, aus der sichtbaren
# Antwort entfernen (beobachtet bei gemma: '<|tool_call>call:...(...)<tool_call|>').
_TOOLCALL_RE = re.compile(r"<\|?\s*tool_call\s*\|?>.*?<\|?\s*/?\s*tool_call\s*\|?>",
                          re.DOTALL | re.IGNORECASE)


def _strip_toolcalls(text: str) -> str:
    t = _TOOLCALL_RE.sub("", text or "")
    t = re.sub(r"<\|?\s*/?\s*tool_call\s*\|?>", "", t, flags=re.IGNORECASE)  # einzelne Reste
    return t.strip()


class State(TypedDict, total=False):
    query: str
    mem_context: str
    retrieved: str
    draft: str
    exec_out: str
    verification: str
    verified: bool
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


# Node heisst 'draft_node' (langgraph verbietet Node-Name == State-Key 'draft'),
# schreibt aber in den Kanal 'draft'.
async def draft(state: State) -> State:
    parts = [
        state.get("mem_context", ""),
        f"Frage:\n{state['query']}",
        f"\nBelege (RAGFlow/Morphik):\n{state.get('retrieved','(keine)')}",
    ]
    if state.get("verification"):
        parts.append("\nWeb-Gegenpruefung MIT BELEG-QUOTEN — ARBEITE EIN: [BESTAETIGT] "
                     "uebernehmen (bei niedriger Quote vorsichtig formulieren), [AKTUELLER] "
                     "den neueren Web-Stand mit Datum/Quelle bevorzugen, [WIDERSPRUCH] beide "
                     "Staende zeigen, [KEINE FUNDE] (0%/0%) die Aussage als UNBELEGTES "
                     "Modellwissen kennzeichnen (kein Fehler, aber NICHT als gesichert "
                     "darstellen):\n"
                     + state["verification"])
    if state.get("critique"):
        parts.append(f"\nVerbessere den vorigen Entwurf gemaess Kritik:\n{state['critique']}")
        parts.append(f"\nVoriger Entwurf:\n{state.get('draft','')}")
    parts.append("\nBrauchst du eine Berechnung/Datei, gib EINEN ```python ...``` Block aus; "
                 "er wird in der Sandbox ausgefuehrt.")
    sys = _SYS_ORCHESTRATED + "\n\nAKTUELLER ZEITBEZUG (WICHTIG)\n" + C.now_context()
    resp = await _llm.ainvoke([{"role": "system", "content": sys},
                               {"role": "user", "content": "\n".join(parts)}])
    return {"draft": _strip_toolcalls(resp.content), "iteration": state.get("iteration", 0) + 1}


async def execute(state: State) -> State:
    blocks = CODE_RE.findall(state.get("draft", ""))
    if not blocks:
        return {"exec_out": ""}
    async with httpx.AsyncClient() as http:
        outs = [await C.t_run_code(http, b) for b in blocks[:2]]
    return {"exec_out": "\n\n".join(outs)[:6000]}


_EXTRACT_SYS = (
    "Du bereitest eine Web-Gegenpruefung vor. Lies Entwurf und Belege und finde die "
    "WENIGEN faktischen/zeitkritischen Aussagen (Betraege, Saetze, Fristen, Rechtsstand, "
    "Versionen, Datumsangaben), deren Aktualitaet/Widerspruchsfreiheit zu pruefen ist. "
    "Gib pro Aussage GENAU EINE kurze, PERSONENFREIE Suchanfrage aus (KEINE Namen, VSNR, "
    "Adressen oder sonstige PII) — eine pro Zeile, hoechstens %d Zeilen, sonst nichts. "
    "Gibt es nichts Pruefbares, antworte mit genau: KEINE"
)

_COMPARE_SYS = (
    "Du quantifizierst die BELEGLAGE — nicht raten, nur die vorgelegten Quellen zaehlen.\n"
    "Vorgelegt: der Entwurf, RAG-Belege (eigene Dokumente, je [Dok]) und Web-Treffer "
    "(je (Domain) + Guete: [vertrauenswuerdig], [vertrauenswuerdig·THEMA], [niedrig] oder "
    "ohne = neutral). Fuer JEDE pruefbare Aussage des Entwurfs gib GENAU diesen Block aus:\n"
    "<knappe Aussage>\n"
    "  RAG: <p>% (<k>/<n> Belege stuetzen | Dok: <Namen oder ->)\n"
    "  Web: <q>% (<j>/<m> Quellen stuetzen, davon <t> vertrauenswuerdig | "
    "Domains: vertrauenswuerdige zuerst)\n"
    "  Fazit: [BESTAETIGT] (VERTRAUENSWUERDIGE Quellen und/oder RAG stuetzen) | "
    "[AKTUELLER] (Web neuer als RAG, mit Datum/Quelle) | "
    "[WIDERSPRUCH] (Quellen uneinig -> beide Staende) | "
    "[KEINE FUNDE] (0%/0% — KEIN Fehler, nur kein Beleg; ruht auf Modellwissen)\n"
    "Gewichtung: [vertrauenswuerdig] zaehlt STARK, [niedrig] schwach. Eine "
    "[vertrauenswuerdig·THEMA]-Quelle zaehlt nur voll, wenn THEMA zur Frage passt — sonst "
    "wie neutral. Stuetzen NUR [niedrig]-Quellen -> NICHT [BESTAETIGT], niedrige Konfidenz.\n"
    "Regeln: Prozente AUSSCHLIESSLICH aus den vorgelegten Quellen. RAG-Quote NUR aus den "
    "vorgelegten RAG-Belegen ([Dok]); gibt es keine, RAG: 0/0 — erfinde KEINE RAG-Dokumente "
    "(insb. KEINE Web-Domains als Dok). Nenne reale Domains/Dok. Keine PII. Knapp. "
    "Nichts Pruefbares -> genau: KEINE"
)


def _queries_from(text: str) -> list[str]:
    out: list[str] = []
    for line in (text or "").splitlines():
        s = _LIST_MARK.sub("", line).strip().strip('"')
        if not s or s.upper() == "KEINE":
            continue
        out.append(s)
    return out[:VERIFY_MAX_QUERIES]


async def verify(state: State) -> State:
    """Deterministische Web-Gegenpruefung: laeuft GENAU EINMAL (Ergebnis wird gecacht,
    damit Revise-Schleifen keine erneuten Websuchen ausloesen)."""
    if state.get("verified"):
        return {}  # bereits geprueft -> keine erneute (gedrosselte) Websuche
    draft_text = state.get("draft", "")
    if not draft_text.strip():
        return {"verified": True, "verification": ""}

    # 1) Pruefwuerdige, PII-freie Suchanfragen bestimmen.
    qresp = await _llm_strict.ainvoke(
        [{"role": "system", "content": C.now_context() + "\n\n" + _EXTRACT_SYS % VERIFY_MAX_QUERIES},
         {"role": "user", "content": f"Entwurf:\n{draft_text}\n\nBelege:\n{state.get('retrieved','')}"}])
    queries = _queries_from(qresp.content)
    if not queries:
        return {"verified": True, "verification": ""}

    # 2) Pro Anfrage Websuche ueber den PII-Masking-Proxy (search_web -> presidio).
    evidence = []
    async with httpx.AsyncClient() as http:
        for q in queries:
            res = await C.t_search_web(http, q)
            evidence.append(f"### Suchanfrage: {q}\n{res}")

    # 3) Entwurf gegen die Treffer abgleichen -> erklaerbare Notizen.
    cresp = await _llm_strict.ainvoke(
        [{"role": "system", "content": C.now_context() + "\n\n" + _COMPARE_SYS},
         {"role": "user", "content": f"Entwurf:\n{draft_text}\n\nWeb-Treffer:\n" + "\n\n".join(evidence)}])
    notes = cresp.content.strip()
    if notes.upper() == "KEINE":
        notes = ""
    return {"verified": True, "verification": notes}


async def critic(state: State) -> State:
    judge = (
        "Bewerte den Entwurf streng. Ist er durch die Belege gedeckt, vollstaendig, "
        "(falls Code) durch das Ausfuehrungsergebnis bestaetigt, UND sind die Befunde der "
        "Web-Gegenpruefung eingearbeitet (Widersprueche benannt, aktuellere Staende uebernommen)? "
        "Antworte in Zeile 1 mit GENAU 'APPROVE' oder 'REVISE', danach eine kurze Begruendung."
    )
    ctx = (f"Frage:\n{state['query']}\n\nBelege:\n{state.get('retrieved','')}\n\n"
           f"Entwurf:\n{state.get('draft','')}\n\nAusfuehrungsergebnis:\n{state.get('exec_out','(keins)')}\n\n"
           f"Web-Gegenpruefung:\n{state.get('verification') or '(keine pruefbaren Aussagen)'}")
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
    b.add_node("verify", verify)
    b.add_node("critic", critic)
    b.add_edge(START, "gather")
    b.add_edge("gather", "draft_node")
    b.add_edge("draft_node", "execute")
    b.add_edge("execute", "verify")
    b.add_edge("verify", "critic")
    b.add_conditional_edges("critic", route, {"revise": "draft_node", "done": END})
    return b.compile()


_graph = _build()


async def run_agent(messages: list[dict], user_id: str = "owui") -> str:
    query = C.extract_query(messages)
    mem_context = C.mem_search(_memory, query, user_id)
    final = await _graph.ainvoke({"query": query, "mem_context": mem_context})
    answer = final.get("draft", "")
    if final.get("verification"):
        answer += f"\n\n---\nWeb-Gegenpruefung:\n{final['verification']}"
    if final.get("exec_out"):
        answer += f"\n\n---\nAusfuehrungsergebnis:\n{final['exec_out']}"
    C.mem_add(_memory, query, answer, user_id)
    return answer
