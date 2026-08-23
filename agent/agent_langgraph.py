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
import base64
from typing import TypedDict

import httpx
from langchain_openai import ChatOpenAI
from langgraph.graph import StateGraph, START, END

import common as C

_memory = C.build_memory()
# extra_body: modellspezifische chat_template_kwargs (z.B. force_nonempty_content
# fuer Nemotron) — None, wenn nichts gesetzt ist. Thinking bleibt hier am
# Modell-Default (der Chat SOLL denken duerfen).
_EXTRA = C.llm_extra_body() or None
_llm = ChatOpenAI(model=C.LLM_MODEL, base_url=C.LLM_BASE_URL, api_key=C.LLM_API_KEY,
                  temperature=0.2, extra_body=_EXTRA)
# Deterministisch (temp 0) fuer die strukturierten Verify-Schritte:
_llm_strict = ChatOpenAI(model=C.LLM_MODEL, base_url=C.LLM_BASE_URL, api_key=C.LLM_API_KEY,
                         temperature=0.0, extra_body=_EXTRA)

CODE_RE = re.compile(r"```python\s+(.*?)```", re.DOTALL)
_LIST_MARK = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s*")  # nur Listenmarker, KEINE Zahl-im-Text
# Naeherungen/Platzhalter statt exakter, code-gezaehlter Werte (Volltext-Zaehlaufgaben):
_APPROX_RE = re.compile(r"<\s*\d|>\s*\d|nicht in Top|Top-?\s*\d+|\bca\.?\s*\d|\bcirca\b|ungef[aä]hr",
                        re.IGNORECASE)
MAX_ITER = int(os.environ.get("VERIFY_MAX_ITER", "3"))
VERIFY_MAX_QUERIES = int(os.environ.get("VERIFY_MAX_QUERIES", "3"))

# Diese Variante orchestriert Retrieval/Web/Code SELBST -> das Modell darf KEINE
# Tools aufrufen (sonst leakt tool_call-Syntax in die sichtbare Antwort). Eigener
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
# Antwort entfernen (Form: '<|tool_call>call:...(...)<tool_call|>').
_TOOLCALL_RE = re.compile(r"<\|?\s*tool_call\s*\|?>.*?<\|?\s*/?\s*tool_call\s*\|?>",
                          re.DOTALL | re.IGNORECASE)


def _strip_toolcalls(text: str) -> str:
    t = _TOOLCALL_RE.sub("", text or "")
    t = re.sub(r"<\|?\s*/?\s*tool_call\s*\|?>", "", t, flags=re.IGNORECASE)  # einzelne Reste
    return t.strip()


def _text(resp) -> str:
    """AIMessage -> str. langchain 1.x kann .content je nach output_version als LISTE
    von Content-Blocks liefern (statt str) -> Text-Blocks extrahieren und joinen."""
    c = getattr(resp, "content", resp)
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        parts = []
        for b in c:
            if isinstance(b, str):
                parts.append(b)
            elif isinstance(b, dict) and b.get("type") in (None, "text"):
                parts.append(b.get("text", ""))
        return "".join(parts)
    return str(c or "")


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
    fulltext: str
    fulldoc_name: str


async def gather(state: State) -> State:
    # RAGFlow UND Morphik greifen immer (claude.ai-Funktionsumfang). Liegt eine
    # Chat-Datei vor (Volltext), wird das Retrieval AUF DIESE DATEI eingegrenzt
    # (only_doc) -> RAGFlow/Morphik liefern Belege/Zitate aus genau dem Dokument,
    # ohne themenfremde Treffer; die Ganz-Dokument-Berechnung laeuft auf document.txt.
    only = state.get("fulldoc_name") if state.get("fulltext") else None
    async with httpx.AsyncClient() as http:
        docs = await C.t_retrieve_documents(http, state["query"], only_doc=only)
        extra = ""
        if C.MORPHIK_API_URL:
            extra = "\n\n[Multimodal]\n" + await C.t_retrieve_multimodal(http, state["query"], only_doc=only)
    retrieved = (docs + extra)[:6000]
    if state.get("fulltext"):
        retrieved = (f"(Volltext-Modus: die KOMPLETTE Datei '{state.get('fulldoc_name','Dokument')}' "
                     f"liegt als document.txt vor. Untenstehende RAG-/Morphik-Belege stammen — soweit "
                     f"schon indiziert — AUS DIESER Datei und dienen als Zitatquelle:)\n" + retrieved)
    return {"retrieved": retrieved, "iteration": 0}


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
        if state.get("exec_out"):
            parts.append("\nAusfuehrungsergebnis des vorigen Codes (ECHTE Daten aus dem "
                         "Dokument — daran orientieren, nicht neu raten):\n"
                         + state["exec_out"][:6000])
    if state.get("fulltext"):
        ft = state["fulltext"]
        cands = C.proper_noun_candidates(ft, 150)
        cand_line = ", ".join(f"{w}:{c}" for w, c in cands) or "(keine erkannt)"
        parts.append(
            f"\nVOLLTEXT-MODUS: Die KOMPLETTE Datei '{state.get('fulldoc_name','Dokument')}' "
            f"({len(ft)} Zeichen) liegt im Sandbox-Arbeitsverzeichnis als 'document.txt'. "
            f"Aufgaben ueber das GANZE Dokument NUR damit loesen, NICHT mit RAG-Schnipseln.\n"
            f"Bei Namens-/Haeufigkeitsaufgaben GENAU SO vorgehen:\n"
            f"1) Rate KEINE Begriffe aus dem Gedaechtnis (Schreibweisen im Text weichen ab: "
            f"Uebersetzungen, Akzente -> falsche 0-Treffer). Waehle Personen/Orte aus den unten "
            f"aus dem Text extrahierten, EXAKT geschriebenen Tokens.\n"
            f"2) NUR echte Eigennamen (konkrete Personen/Orte). KEINE Himmelsrichtungen "
            f"(Norden/Osten/Sueden/Westen), Voelker-/Gattungsbegriffe (Hobbits, Halblinge, Menschen, "
            f"Elben, Zwerge) oder Allgemeinwoerter. Mehrwortnamen (z.B. 'Minas Tirith') als GANZEN "
            f"Namen zaehlen, nicht das Fragment ('Minas').\n"
            f"3) Schreibe GENAU EINEN ```python-Block, der document.txt liest und JEDEN gewaehlten "
            f"Namen EXAKT mit Wortgrenzen zaehlt (re.findall(r'\\b'+re.escape(name)+r'\\b', text)), "
            f"absteigend sortiert und eine CSV speichert. Die Vorschau-Zahlen sind nur ein Hinweis "
            f"und UNVOLLSTAENDIG (nur Top-Tokens) — die endgueltige Tabelle kommt AUS DEM CODE. "
            f"Schreibe NIEMALS '< 100' oder 'nicht in Top-N'; gib fuer JEDEN Namen die exakte Zahl.\n"
            f"Extrahierte Tokens (exakte Schreibweise, Token:Anzahl):\n{cand_line}")
        hint = C.artifact_hint(state.get("fulldoc_name", ""))
        if hint:
            parts.append("\n" + hint)
    parts.append("\nBrauchst du eine Berechnung/Datei, gib EINEN ```python ...``` Block aus; "
                 "er wird in der Sandbox ausgefuehrt.")
    sys = _SYS_ORCHESTRATED + "\n\nAKTUELLER ZEITBEZUG (WICHTIG)\n" + C.now_context()
    resp = await _llm.ainvoke([{"role": "system", "content": sys},
                               {"role": "user", "content": "\n".join(parts)}])
    return {"draft": _strip_toolcalls(_text(resp)), "iteration": state.get("iteration", 0) + 1}


async def execute(state: State) -> State:
    blocks = CODE_RE.findall(state.get("draft", ""))
    if not blocks:
        return {"exec_out": ""}
    files = None
    if state.get("fulltext"):
        files = {"document.txt": base64.b64encode(
            state["fulltext"].encode("utf-8")).decode("ascii")}
    async with httpx.AsyncClient() as http:
        outs = [await C.t_run_code(http, b, files=files) for b in blocks[:2]]
    return {"exec_out": "\n\n".join(outs)[:16000]}


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
    queries = _queries_from(_text(qresp))
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
    notes = _text(cresp).strip()
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
    resp = await _llm.ainvoke([{"role": "system", "content": C.now_context() + "\n\n" + judge},
                               {"role": "user", "content": ctx}])
    text = _text(resp).strip()
    approved = text.upper().startswith("APPROVE") or state.get("iteration", 0) >= MAX_ITER
    # Sicherheitsnetz Volltext: Naeherungen/Platzhalter ('< 100', 'nicht in Top-N', 'ca.')
    # statt exakter, code-gezaehlter Werte -> Revise erzwingen (bis MAX_ITER).
    if (state.get("fulltext") and approved and state.get("iteration", 0) < MAX_ITER
            and _APPROX_RE.search(state.get("draft", ""))):
        approved = False
        text = ("REVISE\nVolltext-Modus: Die Tabelle enthaelt Naeherungen/Platzhalter "
                "('< N', 'nicht in Top-N', 'ca.') statt exakter Werte. Zaehle JEDEN genannten "
                "Namen EXAKT per ```python gegen document.txt (re.findall mit Wortgrenzen) und "
                "gib fuer ALLE Eintraege ganzzahlige Werte aus.")
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


async def run_agent(messages: list[dict], user_id: str = "owui",
                    request_body: dict | None = None) -> str:
    query = C.extract_query(messages)
    C.reset_run_files(request_body)   # Sandbox-Dateien pro Anfrage frisch sammeln (+ Chat-ID)
    C.schedule_ingest(request_body or {})  # Chat-Upload lokal nach RAGFlow/Morphik (nicht-blockierend)
    mem_context = C.mem_search(_memory, query, user_id)
    init: dict = {"query": query, "mem_context": mem_context}
    doc = C.read_full_document(request_body or {})  # Volltext der angehaengten Datei
    if doc:
        init["fulldoc_name"], init["fulltext"] = doc
    final = await _graph.ainvoke(init)
    answer = final.get("draft", "")
    if final.get("verification"):
        answer += f"\n\n---\nWeb-Gegenpruefung:\n{final['verification']}"
    if final.get("exec_out"):
        answer += f"\n\n---\nAusfuehrungsergebnis:\n{final['exec_out']}"
    C.mem_add(_memory, query, answer, user_id)
    # In der Sandbox erzeugte Dateien anhaengen (unsichtbarer Block -> OWUI-Filter
    # macht daraus Download-Chips). NACH mem_add, damit kein base64 ins Gedaechtnis geht.
    return answer + C.run_files_block()
