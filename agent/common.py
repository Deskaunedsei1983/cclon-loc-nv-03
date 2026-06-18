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

SEARCH_URL = os.environ.get("SEARCH_URL", "http://presidio-proxy:8080/search")
SANDBOX_RUN_URL = os.environ.get("SANDBOX_RUN_URL", "http://code-sandbox:8000/run")
# microVM-Executor (Microsandbox). Default: Host-Dienst via host.docker.internal.
# Container-Variante: http://microsandbox-executor:8077/run
MSB_EXECUTOR_URL = os.environ.get("MSB_EXECUTOR_URL", "http://host.docker.internal:8077/run")

SYSTEM_PROMPT = """Du bist ein praeziser Engineering-Assistent fuer einen
oesterreichischen Daten-Ingenieur (Sozialversicherungs-Domaene). Antworte auf Deutsch.

Arbeitsweise:
- Fuer Fragen zu internen Dokumenten/Daten ZUERST 'retrieve_documents' (RAGFlow)
  und ggf. 'retrieve_multimodal' (Morphik, bild-/tabellenlastig) nutzen; gruende
  die Antwort auf den Belegen. Erfinde nichts.
- 'search_web' nur fuer aktuelle externe Infos (Anfrage wird PII-maskiert; gib
  trotzdem NIE Klarnamen/VSNR/personenbezogene Daten ein).
- Fuer Berechnungen, Datenanalyse oder das Erzeugen von Dateien 'run_code' nutzen
  (Python; polars/duckdb bevorzugt; python-docx/openpyxl/python-pptx/nbformat da).
  Behaupte ein Ergebnis nie, ohne den Code ausgefuehrt zu haben.
Sei knapp und konkret. Nenne erzeugte Dateinamen am Ende.
"""


# --- Mem0 -------------------------------------------------------------------
def build_memory():
    from mem0 import Memory
    config = {
        "llm": {"provider": "openai", "config": {
            "model": LLM_MODEL, "openai_base_url": LLM_BASE_URL,
            "api_key": LLM_API_KEY, "temperature": 0.1}},
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


async def t_retrieve_multimodal(http: httpx.AsyncClient, query: str) -> str:
    """Morphik-Retrieval fuer bild-/tabellenlastige Dokumente (optional)."""
    if not MORPHIK_API_URL:
        return "Morphik nicht konfiguriert."
    # [VERIFY] Morphik-Endpoint/Payload gegen deine Version pruefen.
    url = f"{MORPHIK_API_URL}/retrieve/chunks"
    headers = {"Authorization": f"Bearer {MORPHIK_API_KEY}"} if MORPHIK_API_KEY else {}
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
        return "\n".join(
            f"- {x.get('title','')} | {x.get('url','')}\n  {x.get('content','')}"
            for x in results[:5]
        )[:5000]
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
