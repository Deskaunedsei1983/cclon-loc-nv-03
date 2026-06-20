"""
common.py — geteilte Basis fuer beide Agent-Varianten
=====================================================
- Konfiguration aus Umgebung
- Mem0 (Vektor-only, Qdrant on-disk, lokales LLM+Embedder)
- die drei (+1) Tool-Funktionen als schlichte async-Funktionen:
    retrieve_documents   -> RAGFlow
    retrieve_multimodal  -> Morphik (optional)
    search_web           -> Presidio-Masking-Proxy
    run_code             -> luftdichte Code-Sandbox
Sowohl agent_pydantic.py (PydanticAI-Tools) als auch agent_langgraph.py
(Critic-Loop) nutzen exakt diese Funktionen -> identisches Verhalten.
"""

from __future__ import annotations

import os
import logging
import datetime
from urllib.parse import urlparse

import httpx

# Telemetrie hart aus, BEVOR mem0 importiert wird:
os.environ.setdefault("MEM0_TELEMETRY", "False")
os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")
os.environ.setdefault("OPENAI_API_KEY", os.environ.get("LLM_API_KEY", "not-needed"))

# Log-Level per ENV (Default INFO; im Stack via AGENT_LOG_LEVEL/LOG_LEVEL auf DEBUG).
_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=getattr(logging, _LEVEL, logging.INFO))
log = logging.getLogger("agent.common")

# --- Konfiguration ----------------------------------------------------------
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "http://vllm-main:5568/v1")
LLM_MODEL = os.environ.get("LLM_MODEL", "main")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "not-needed")

# Mem0-Faktenextraktion braucht STRUKTURIERTE Ausgaben (JSON). DiffusionGemma
# (main-gemma) unterstuetzt KEINE structured outputs -> 400 ValueError. Darum
# laeuft mem0 per Default auf dem autoregressiven Helfer (qwen-helper); per ENV
# umstellbar (z.B. auf main, wenn das Hauptmodell autoregressiv ist).
MEM0_LLM_BASE_URL = os.environ.get("MEM0_LLM_BASE_URL", "http://vllm-helper:30001/v1")
MEM0_LLM_MODEL = os.environ.get("MEM0_LLM_MODEL", "qwen-helper")

EMBED_BASE_URL = os.environ.get("EMBED_BASE_URL", "http://embeddings:80/v1")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "BAAI/bge-m3")
EMBED_DIMS = int(os.environ.get("EMBED_DIMS", "1024"))

QDRANT_HOST = os.environ.get("QDRANT_HOST", "qdrant")
QDRANT_PORT = int(os.environ.get("QDRANT_PORT", "6333"))
MEM0_COLLECTION = os.environ.get("MEM0_COLLECTION", "mem0_main")

RAGFLOW_API_URL = os.environ.get("RAGFLOW_API_URL", "").rstrip("/")
RAGFLOW_API_KEY = os.environ.get("RAGFLOW_API_KEY", "")
RAGFLOW_DATASET_IDS = [s for s in os.environ.get("RAGFLOW_DATASET_IDS", "").split(",") if s.strip()]

MORPHIK_API_URL = os.environ.get("MORPHIK_API_URL", "").rstrip("/")
MORPHIK_API_KEY = os.environ.get("MORPHIK_API_KEY", "")
# Morphik verlangt einen HS256-JWT (signiert mit JWT_SECRET_KEY == WEBUI_SECRET_KEY);
# ein roher Key fuehrt zu 401 "Not enough segments". Wir erzeugen den Token selbst.
MORPHIK_JWT_SECRET = os.environ.get("MORPHIK_JWT_SECRET", "")
MORPHIK_ENTITY_ID = os.environ.get("MORPHIK_ENTITY_ID", "agent")
MORPHIK_JWT_TTL = int(os.environ.get("MORPHIK_JWT_TTL", "86400"))

SEARCH_URL = os.environ.get("SEARCH_URL", "http://presidio-proxy:8080/search")
# Wie viele Web-Treffer der Agent AUSWERTET (mehr Quellen -> Beleg-Quoten zaehlbar).
# Die SearXNG-Engines selbst NICHT anfassen; das hier ist rein agentseitig.
WEB_MAX_RESULTS = int(os.environ.get("WEB_MAX_RESULTS", "12"))

# --- Domain-Vertrauensliste (gewichtete, themen-bewusste Web-Quoten) ---------
# Hauptliste: Datei (gemountet, live pflegbar). Format "domain [tier] [topics]";
# .env ergaenzt via TRUST_DOMAINS / LOW_TRUST_DOMAINS (Thema '*'). Subdomains
# matchen den Parent (gv.at -> x.gv.at); '*.xy.com' == 'xy.com'.
TRUST_DOMAINS_FILE = os.environ.get("TRUST_DOMAINS_FILE", "/app/trust_domains.txt")


def _norm_dom(d: str) -> str:
    d = d.strip().lower().lstrip(".")
    return d[2:] if d.startswith("*.") else d


def _load_trust():
    # domain -> (tier, frozenset(topics));  '*' = alle Themen
    m: "dict[str, tuple[str, frozenset]]" = {}
    for d in os.environ.get("TRUST_DOMAINS", "").split(","):
        if d.strip():
            m[_norm_dom(d)] = ("trusted", frozenset({"*"}))
    for d in os.environ.get("LOW_TRUST_DOMAINS", "").split(","):
        if d.strip():
            m[_norm_dom(d)] = ("low", frozenset({"*"}))
    try:
        with open(TRUST_DOMAINS_FILE, encoding="utf-8") as f:
            for raw in f:
                line = raw.split("#", 1)[0].strip()
                if not line:
                    continue
                parts = line.split()
                dom = _norm_dom(parts[0])
                tier = "low" if (len(parts) > 1 and parts[1].lower().startswith("low")) else "trusted"
                topics = (frozenset(t.strip().lower() for t in parts[2].split(",") if t.strip())
                          if len(parts) > 2 else frozenset({"*"}))
                m[dom] = (tier, topics or frozenset({"*"}))
    except FileNotFoundError:
        pass
    return m


_TRUST_MAP = _load_trust()
log.info("Trust-Liste geladen: %d Domains (%s)", len(_TRUST_MAP), TRUST_DOMAINS_FILE)


def domain_trust(dom: str):
    """(tier, topics) — tier in trusted|low|neutral. Spezifischster (laengster)
    Treffer gewinnt; Subdomains matchen den Parent."""
    dom = (dom or "").lower().lstrip(".")
    best_key, best_val = "", ("neutral", frozenset())
    for entry, val in _TRUST_MAP.items():
        if (dom == entry or dom.endswith("." + entry)) and len(entry) > len(best_key):
            best_key, best_val = entry, val
    return best_val
SANDBOX_RUN_URL = os.environ.get("SANDBOX_RUN_URL", "http://code-sandbox:8000/run")
# microVM-Executor (Microsandbox). Default: Host-Dienst via host.docker.internal.
# Container-Variante: http://microsandbox-executor:8077/run
MSB_EXECUTOR_URL = os.environ.get("MSB_EXECUTOR_URL", "http://host.docker.internal:8077/run")

SYSTEM_PROMPT = """Du bist ein praeziser Engineering-Assistent fuer einen
oesterreichischen Daten-Ingenieur (Sozialversicherungs-Domaene). Antworte auf
Deutsch, knapp und konkret.

WERKZEUGE & ABLAUF
1) INTERNE FAKTEN ZUERST aus dem RAG:
   - 'retrieve_documents' (RAGFlow) fuer text-/dokumentlastige Fragen.
   - 'retrieve_multimodal' (Morphik) fuer BILD-/TABELLEN-/Scan-lastige Dokumente
     (komplexe Layouts, Diagramme, gescannte PDFs). Bei Unsicherheit beide nutzen.
   Gruende die Antwort ausschliesslich auf diesen Belegen. Erfinde nichts.

2) GEGENPRUEFUNG IM WEB (Kernaufgabe):
   Fuer faktische oder zeitkritische Aussagen aus dem RAG (Betraege, Saetze,
   Fristen, Rechtsstand, Versionen, "ab/seit ...") pruefe mit 'search_web', ob die
   RAG-Info AKTUELL ist und ob es WIDERSPRUECHE gibt.
   - Destilliere dafuer NUR die sachliche, allgemeine Aussage in eine kurze
     Suchanfrage (z.B. "Hoechstbeitragsgrundlage ASVG 2026").
   - PII-DISZIPLIN: Gib NIE Klarnamen, VSNR, Adressen oder andere personenbezogene
     Daten aus dem RAG in die Websuche. Die Anfrage wird zusaetzlich serverseitig
     maskiert, aber formuliere von vornherein personenfrei.

3) ABGLEICH & KENNZEICHNUNG:
   - Web bestaetigt das RAG -> knapp bestaetigen.
   - Web ist neuer/abweichend -> AKTUELLEN Stand samt Datum/Quelle nennen und die
     RAG-Stelle als moeglicherweise veraltet markieren.
   - Widerspruch -> beide Staende explizit gegenueberstellen, nicht stillschweigend
     ueberschreiben.

4) RECHNEN/DATEIEN: 'run_code' (Python; polars/duckdb bevorzugt;
   python-docx/openpyxl/python-pptx/nbformat vorhanden). Behaupte ein Ergebnis nie,
   ohne den Code ausgefuehrt zu haben.

Smalltalk/triviale Fragen ohne Faktenbezug: ohne Tools direkt antworten.
Nenne am Ende die Quellen (RAG-Dokument + ggf. URL) und erzeugte Dateinamen.
"""


# --- Aktueller Zeitbezug (pro Anfrage zur Laufzeit!) ------------------------
# Ein LLM hat KEINE Uhr -> ohne echtes Datum raet es seinen Trainings-Cutoff.
# Wir injizieren das reale Datum bei JEDEM Request (nicht beim Import, sonst
# friert es auf den Container-Start ein). Ankert heute/gestern/aktuell/usw.
try:
    from zoneinfo import ZoneInfo
    _TZ = ZoneInfo(os.environ.get("AGENT_TZ", "Europe/Vienna"))
except Exception:  # tzdata fehlt im Image -> UTC
    _TZ = datetime.timezone.utc


def now_context() -> str:
    now = datetime.datetime.now(_TZ)
    return (
        f"Heutiges Datum/Uhrzeit (Laufzeit): {now:%A, %Y-%m-%d %H:%M %Z}. "
        "Rechne ALLE relativen Zeitangaben (heute, gestern, morgen, aktuell, "
        "dieses Jahr, 'vor 10 Jahren', 'ist X schon vorbei') von DIESEM Zeitpunkt "
        "aus; nutze NICHT dein Trainingswissen als 'jetzt'. Bei laufenden oder "
        "kuerzlich gestarteten Ereignissen NICHT annehmen, etwas habe noch nicht "
        "stattgefunden; fehlende Live-Daten als offen/ungeprueft kennzeichnen."
    )


def system_prompt_now() -> str:
    """SYSTEM_PROMPT + frischer Zeitbezug. In den Agenten IMMER statt des rohen
    SYSTEM_PROMPT verwenden, damit das Datum pro Anfrage stimmt."""
    return SYSTEM_PROMPT + "\n\nAKTUELLER ZEITBEZUG (WICHTIG)\n" + now_context()


# --- Mem0 -------------------------------------------------------------------
def build_memory():
    from mem0 import Memory
    config = {
        "llm": {"provider": "openai", "config": {
            # NICHT das (evtl. diffusionsbasierte) Hauptmodell: mem0 fordert JSON.
            "model": MEM0_LLM_MODEL, "openai_base_url": MEM0_LLM_BASE_URL,
            # top_p explizit: mem0/vLLM-Default 0.0 -> vLLM lehnt ab
            # ("top_p must be in (0, 1]") -> Mem0-Add schlug fehl.
            "api_key": LLM_API_KEY, "temperature": 0.1, "top_p": 1.0}},
        "embedder": {"provider": "openai", "config": {
            "model": EMBED_MODEL, "openai_base_url": EMBED_BASE_URL,
            "api_key": "not-needed", "embedding_dims": EMBED_DIMS}},
        "vector_store": {"provider": "qdrant", "config": {
            "host": QDRANT_HOST, "port": QDRANT_PORT,
            "collection_name": MEM0_COLLECTION,
            "embedding_model_dims": EMBED_DIMS, "on_disk": True}},
        # kein graph_store -> spart RAM (kein Neo4j/Memgraph)
    }
    try:
        m = Memory.from_config(config)
        log.info("Mem0 ok (Qdrant on-disk, vektor-only).")
        return m
    except Exception:
        log.exception("Mem0-Init fehlgeschlagen -> ohne Gedaechtnis.")
        return None


def mem_search(memory, query: str, user_id: str) -> str:
    if not memory or not query:
        return ""
    try:
        hits = memory.search(query=query, user_id=user_id, limit=5)
        items = hits.get("results", hits) if isinstance(hits, dict) else hits
        facts = [h.get("memory", "") for h in items][:5]
        if facts:
            return "Bekanntes aus frueheren Sitzungen:\n- " + "\n- ".join(facts) + "\n\n"
    except Exception:
        log.exception("Mem0-Suche fehlgeschlagen (ignoriert)")
    return ""


def mem_add(memory, query: str, answer: str, user_id: str) -> None:
    if not memory or not query:
        return
    try:
        memory.add(messages=[{"role": "user", "content": query},
                             {"role": "assistant", "content": answer}],
                   user_id=user_id)
    except Exception:
        log.exception("Mem0-Add fehlgeschlagen (ignoriert)")


def extract_query(messages: list[dict]) -> str:
    user_msgs = [m for m in messages if m.get("role") == "user"]
    q = user_msgs[-1]["content"] if user_msgs else ""
    if isinstance(q, list):
        q = " ".join(p.get("text", "") for p in q if isinstance(p, dict))
    return q


# --- Tool-Funktionen (schlicht, von beiden Varianten genutzt) ---------------
async def t_retrieve_documents(http: httpx.AsyncClient, query: str) -> str:
    """RAGFlow-Retrieval. [VERIFY] Endpoint/Payload je RAGFlow-Version."""
    if not RAGFLOW_API_URL or not RAGFLOW_API_KEY:
        return "Kein RAGFlow konfiguriert (RAGFLOW_API_KEY fehlt)."
    url = f"{RAGFLOW_API_URL}/api/v1/retrieval"
    payload = {"question": query, "dataset_ids": RAGFLOW_DATASET_IDS, "page_size": 8}
    headers = {"Authorization": f"Bearer {RAGFLOW_API_KEY}"}
    try:
        r = await http.post(url, json=payload, headers=headers, timeout=30.0)
        r.raise_for_status()
        data = r.json()
        chunks = (data.get("data") or {}).get("chunks") or data.get("chunks") or []
        if not chunks:
            return "Keine relevanten Dokumente gefunden."
        out = []
        for c in chunks[:8]:
            content = c.get("content") or c.get("content_with_weight") or ""
            doc = c.get("document_keyword") or c.get("docnm_kwd") or "?"
            out.append(f"[{doc}] {content}")
        return "\n\n".join(out)[:6000]
    except Exception as e:
        log.exception("RAGFlow-Retrieval fehlgeschlagen")
        return f"Retrieval-Fehler: {e}"


def morphik_auth_header() -> dict:
    """Bearer-Header fuer Morphik. Bevorzugt einen selbst signierten HS256-JWT
    (Dev-Token: type=developer, permissions read/write/admin), weil Morphik einen
    JWT erwartet — ein roher Key ergibt 401 'Not enough segments'.
    [VERIFY] Claims/Algorithmus gegen deine Morphik-Version pruefen."""
    if MORPHIK_JWT_SECRET:
        try:
            import time
            import jwt  # PyJWT
            token = jwt.encode(
                {"type": "developer", "entity_id": MORPHIK_ENTITY_ID,
                 "permissions": ["read", "write", "admin"],
                 "exp": int(time.time()) + MORPHIK_JWT_TTL},
                MORPHIK_JWT_SECRET, algorithm="HS256")
            return {"Authorization": f"Bearer {token}"}
        except Exception:
            log.exception("Morphik-JWT-Erzeugung fehlgeschlagen -> Fallback")
    if MORPHIK_API_KEY:
        return {"Authorization": f"Bearer {MORPHIK_API_KEY}"}
    return {}


async def t_retrieve_multimodal(http: httpx.AsyncClient, query: str) -> str:
    """Morphik-Retrieval fuer bild-/tabellenlastige Dokumente (optional)."""
    if not MORPHIK_API_URL:
        return "Morphik nicht konfiguriert."
    # [VERIFY] Morphik-Endpoint/Payload gegen deine Version pruefen.
    url = f"{MORPHIK_API_URL}/retrieve/chunks"
    headers = morphik_auth_header()
    try:
        r = await http.post(url, json={"query": query, "k": 6}, headers=headers, timeout=40.0)
        r.raise_for_status()
        data = r.json()
        chunks = data.get("chunks") or data.get("results") or data
        if isinstance(chunks, list) and chunks:
            return "\n\n".join(str(c.get("content", c))[:800] for c in chunks[:6])[:6000]
        return "Keine multimodalen Treffer."
    except Exception as e:
        log.exception("Morphik-Retrieval fehlgeschlagen")
        return f"Morphik-Fehler: {e}"


async def t_search_web(http: httpx.AsyncClient, query: str) -> str:
    """Websuche ueber den PII-Masking-Proxy."""
    try:
        r = await http.get(SEARCH_URL, params={"q": query, "format": "json"}, timeout=25.0)
        r.raise_for_status()
        results = r.json().get("results", [])
        if not results:
            return "Keine Suchergebnisse."
        lines = []
        for x in results[:WEB_MAX_RESULTS]:
            url = x.get("url", "")
            dom = urlparse(url).netloc.replace("www.", "") or "?"
            tier, topics = domain_trust(dom)
            if tier == "trusted":
                tt = "" if ("*" in topics or not topics) else "·" + ",".join(sorted(topics))
                mark = f" [vertrauenswuerdig{tt}]"
            elif tier == "low":
                mark = " [niedrig]"
            else:
                mark = ""
            lines.append(f"- ({dom}{mark}) {x.get('title','')} | {url}\n  {x.get('content','')}")
        return "\n".join(lines)[:7000]
    except Exception as e:
        log.exception("Websuche fehlgeschlagen")
        return f"Such-Fehler: {e}"


async def _post_run(http: httpx.AsyncClient, url: str, code: str) -> dict:
    r = await http.post(url, json={"code": code}, timeout=180.0)
    r.raise_for_status()
    return r.json()


async def t_run_code(http: httpx.AsyncClient, code: str) -> str:
    """Python ausfuehren — bevorzugt in der Microsandbox-microVM (hardware-isoliert),
    Fallback auf die luftdichte Subprozess-Sandbox (transparent gekennzeichnet)."""
    engine = "microVM (Microsandbox)"
    res = None
    if MSB_EXECUTOR_URL:
        try:
            res = await _post_run(http, MSB_EXECUTOR_URL, code)
            if res.get("returncode") == -1 and "SDK nicht installiert" in (res.get("stderr") or ""):
                res = None  # Executor laeuft, aber SDK fehlt -> Fallback
        except Exception:
            log.warning("microVM-Executor nicht erreichbar -> Fallback auf Subprozess-Sandbox")
            res = None
    if res is None:
        engine = "Subprozess-Sandbox (FALLBACK, kein microVM!)"
        try:
            res = await _post_run(http, SANDBOX_RUN_URL, code)
        except Exception as e:
            log.exception("Code-Ausfuehrung fehlgeschlagen")
            return f"Sandbox-Fehler: {e}"

    files = ", ".join(f"{f['name']} ({f['size']}B)" for f in res.get("files", [])) or "keine"
    return (f"[Engine: {engine}] returncode={res.get('returncode')}\n"
            f"--- stdout ---\n{res.get('stdout','')}\n"
            f"--- stderr ---\n{res.get('stderr','')}\n"
            f"--- Dateien ---\n{files}  (Pfad: {res.get('work_dir','')})")
