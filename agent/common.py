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
import asyncio
import collections
import datetime
import base64
import glob
import html
import io
import re
import time
import zipfile
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
# mem0 braucht JSON/structured outputs. 'auto' (Default): Helfer bevorzugt, sonst
# autoregressives Hauptmodell (Gemma-Diffusion wird uebersprungen), sonst aus.
# Feste Wahl moeglich: MEM0_LLM_BASE_URL=http://vllm-helper:30001/v1 + MEM0_LLM_MODEL.
MEM0_LLM_BASE_URL = os.environ.get("MEM0_LLM_BASE_URL", "auto")
MEM0_LLM_MODEL = os.environ.get("MEM0_LLM_MODEL", "qwen-helper")
# Optionaler CPU-Struct-Sidecar (llama.cpp, ~0 VRAM, garantiertes JSON via GBNF).
# Im 'auto'-Modus ZUERST probiert -> nimmt mem0 ganz von der GPU. Profil "mem0struct".
MEM0_STRUCT_URL = os.environ.get("MEM0_STRUCT_URL", "http://mem0-struct:8088/v1")
MEM0_STRUCT_MODEL = os.environ.get("MEM0_STRUCT_MODEL", "mem0-struct")
# Memory hart abschaltbar; sonst auto-deaktiviert, wenn das MEM0-LLM nicht erreichbar ist.
MEM0_ENABLED = os.environ.get("MEM0_ENABLED", "true").strip().lower() not in ("0", "false", "no", "off")
# mem0s 2-Call-"Memory-Manager" (infer=True) ist mit kleinen lokalen Modellen fragil
# (liefert fuer seinen langen Prompt teils leeres JSON -> Add schlaegt fehl, char 0).
# Default infer=False: robustes Direkt-Speichern der User-Aussage (Embedding -> Qdrant),
# KEIN 2. LLM-Call. infer=True nur mit einem zuverlaessigen JSON-LLM sinnvoll.
MEM0_INFER = os.environ.get("MEM0_INFER", "false").strip().lower() in ("1", "true", "yes", "on")

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


# --- Auto-Blocklist (low-Tier) ----------------------------------------------
# Der Agent LIEST nur eine Datei (vom 'blocklist-fetcher'-Sidecar gepflegt) -> KEIN
# Internet-Egress im Agent (PII/DSGVO: kein User-Datenpfad nach draussen). Kuratierte
# _TRUST_MAP-Eintraege haben VORRANG (explizit Vertrautes wird nie demoted).
BLOCKLIST_FILE = os.environ.get("BLOCKLIST_FILE", "/app/blocklist/blocklist.txt")
BLOCKLIST_REFRESH_MIN = int(os.environ.get("BLOCKLIST_REFRESH_MIN", "60"))
_BLOCKLIST: "set[str]" = set()


def _in_blocklist(dom: str) -> bool:
    if not _BLOCKLIST:
        return False
    parts = dom.split(".")
    return any(".".join(parts[i:]) in _BLOCKLIST for i in range(len(parts) - 1))


def _parse_blocklist(text: str) -> "set[str]":
    out: "set[str]" = set()
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        dom = line.split()[-1].lower().strip(".")  # hosts "0.0.0.0 domain" ODER "domain"
        if dom and "." in dom and not dom.replace(".", "").isdigit() and dom != "localhost":
            out.add(dom)
    return out


def load_blocklist() -> int:
    """Liest die vom 'blocklist-fetcher'-Sidecar gepflegte Datei (KEIN Netzzugriff
    im Agent). Fehlt sie -> leere Liste. Wird periodisch neu gelesen."""
    global _BLOCKLIST
    try:
        with open(BLOCKLIST_FILE, encoding="utf-8", errors="ignore") as f:
            _BLOCKLIST = _parse_blocklist(f.read())
        log.info("Blocklist geladen: %d Domains (%s)", len(_BLOCKLIST), BLOCKLIST_FILE)
    except FileNotFoundError:
        _BLOCKLIST = set()
    except Exception as e:
        log.warning("Blocklist-Datei nicht lesbar (%s): %s", BLOCKLIST_FILE, e)
    return len(_BLOCKLIST)


def domain_trust(dom: str):
    """(tier, topics) — tier in trusted|low|neutral. Kuratierte Liste hat Vorrang,
    dann die Auto-Blocklist (low). Spezifischster Treffer gewinnt; Subdomains erben."""
    dom = (dom or "").lower().lstrip(".")
    best_key, best_val = "", None
    for entry, val in _TRUST_MAP.items():
        if (dom == entry or dom.endswith("." + entry)) and len(entry) > len(best_key):
            best_key, best_val = entry, val
    if best_val is not None:
        return best_val
    if _in_blocklist(dom):
        return ("low", frozenset({"*"}))
    return ("neutral", frozenset())
SANDBOX_RUN_URL = os.environ.get("SANDBOX_RUN_URL", "http://code-sandbox:8000/run")
# microVM-Executor (Microsandbox). Default: Host-Dienst via host.docker.internal.
# Container-Variante: http://microsandbox-executor:8077/run
MSB_EXECUTOR_URL = os.environ.get("MSB_EXECUTOR_URL", "http://host.docker.internal:8077/run")

# Volltext-Modus: OWUIs Upload-Volume read-only gemountet -> ganze Dateien lesbar
# (umgeht den OWUI-0.9.5-401). OWUI legt Uploads unter <data>/uploads/{id}_{name} ab.
OWUI_DATA_DIR = os.environ.get("OWUI_DATA_DIR", "/owui-data")
FULLDOC_MAX_BYTES = int(os.environ.get("FULLDOC_MAX_BYTES", str(30 * 1024 * 1024)))
# Auto-Ingest: frische Chat-Uploads selbst an den ingest-router-Sidecar schicken
# (klassifiziert -> RAGFlow/Morphik). REIN LOKAL (interner Dienst, KEIN Internet-Egress)
# -> DSGVO-konform und macht den fragilen OWUI-Ingest-Filter ueberfluessig: der Agent
# hat die Datei-Bytes (Volltext-Volume) ohnehin schon.
INGEST_ROUTER_URL = os.environ.get("INGEST_ROUTER_URL", "http://ingest-router:8000").rstrip("/")
AGENT_AUTO_INGEST = os.environ.get("AGENT_AUTO_INGEST", "true").strip().lower() not in (
    "0", "false", "no", "off")
_INGESTED: "set[str]" = set()  # bereits angestossene Uploads (pro Prozess, idempotent)
_INGEST_TASKS: set = set()      # Referenzen halten -> Hintergrund-Tasks nicht vorzeitig GC'en
# Fallback-Zeitfenster: schickt OWUI keine Datei-Referenz, gilt der juengste Upload
# innerhalb dieser Spanne als die gemeinte Datei (Default 15 min).
RECENT_UPLOAD_MAX_AGE_S = int(os.environ.get("RECENT_UPLOAD_MAX_AGE_S", "900"))

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
def _models_list(base_url: str):
    """Liste der model-ids von /v1/models, oder None bei DNS/Connect-Fehler."""
    try:
        r = httpx.get(base_url.rstrip("/") + "/models", timeout=3.0)
        r.raise_for_status()
        return [m.get("id", "") for m in r.json().get("data", [])]
    except Exception:
        return None


def _pick_mem0_llm():
    """(base_url, model) fuer mem0 ODER None. mem0 braucht JSON/structured outputs.
    'auto'-Reihenfolge: CPU-Struct-Sidecar (garantiertes JSON, ~0 VRAM) -> GPU-Helfer
    -> aktives autoregressives Hauptmodell (Gemma-Diffusion kann kein JSON, an den
    served-model-names erkannt -> uebersprungen) -> None. Explizite Wahl hat Vorrang."""
    if MEM0_LLM_BASE_URL and MEM0_LLM_BASE_URL.lower() != "auto":
        return MEM0_LLM_BASE_URL, MEM0_LLM_MODEL
    if MEM0_STRUCT_URL and _models_list(MEM0_STRUCT_URL) is not None:
        return MEM0_STRUCT_URL, MEM0_STRUCT_MODEL
    helper = "http://vllm-helper:30001/v1"
    if _models_list(helper) is not None:
        return helper, "qwen-helper"
    ids = _models_list(LLM_BASE_URL)
    if ids is not None and not any("gemma" in i.lower() for i in ids):
        return LLM_BASE_URL, LLM_MODEL  # autoregressives Hauptmodell ("main")
    return None


def build_memory():
    if not MEM0_ENABLED:
        log.info("Mem0 deaktiviert (MEM0_ENABLED=false) -> ohne Gedaechtnis.")
        return None
    picked = _pick_mem0_llm()
    if picked is None:
        log.warning("Mem0: kein JSON-faehiges LLM erreichbar (Helfer aus UND Hauptmodell "
                    "diffusionsbasiert/aus?) -> Memory deaktiviert.")
        return None
    mem_url, mem_model = picked
    log.info("Mem0-LLM gewaehlt: %s (%s)", mem_model, mem_url)
    from mem0 import Memory
    config = {
        "llm": {"provider": "openai", "config": {
            "model": mem_model, "openai_base_url": mem_url,
            # top_p explizit: mem0/vLLM-Default 0.0 -> vLLM lehnt ab ("must be in (0,1]").
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
    except Exception as e:
        log.warning("Mem0-Suche fehlgeschlagen (ignoriert): %s", e)
    return ""


def mem_add(memory, query: str, answer: str, user_id: str) -> None:
    if not memory or not query:
        return
    try:
        if MEM0_INFER:
            memory.add(messages=[{"role": "user", "content": query},
                                 {"role": "assistant", "content": answer}],
                       user_id=user_id)
        else:
            # infer=False: KEIN fragiler 2-Call-Memory-Manager. Speichert die
            # User-Aussage direkt (Embedding -> Qdrant), robust + deterministisch.
            memory.add(messages=[{"role": "user", "content": query}],
                       user_id=user_id, infer=False)
    except Exception as e:
        log.warning("Mem0-Add fehlgeschlagen (ignoriert): %s", e)


def extract_query(messages: list[dict]) -> str:
    user_msgs = [m for m in messages if m.get("role") == "user"]
    q = user_msgs[-1]["content"] if user_msgs else ""
    if isinstance(q, list):
        q = " ".join(p.get("text", "") for p in q if isinstance(p, dict))
    return owui_real_query(q)  # bei OWUI-RAG-Template die echte Frage herausziehen


# --- OWUI-Hintergrundtasks (Titel/Tags/Query-/Follow-up-Generierung) ---------
# OWUI nutzt fuer EXTERNE (OpenAI-API-)Modelle TASK_MODEL_EXTERNAL; ist der nicht
# erreichbar (mem0-struct-Profil aus), faellt es auf das CHAT-Modell zurueck und
# schickt research-agent seine internen Task-Prompts. Die sollen NICHT durch die
# Such-/Critic-Pipeline (sonst Websuche/Code fuer einen Chat-Titel).
_OWUI_TASK_RE = re.compile(r"^\s*###\s*Task:", re.IGNORECASE)
# ACHTUNG: OWUIs RAG-ANTWORT-Template ("Respond to the user query using the provided
# context") startet AUCH mit '### Task:', traegt aber den Dokument-Kontext
# (<context>/<source resource-id=...>) und IST die echte Nutzerfrage -> darf NICHT
# kurzgeschlossen werden (sonst laeuft der Volltext-Modus nie). Daran trennen.
_OWUI_CTX_RE = re.compile(r"<context>|<source\b|resource-id=", re.IGNORECASE)
_OWUI_USERQ_RE = re.compile(r"<user_query>\s*(.*?)\s*</user_query>", re.IGNORECASE | re.DOTALL)


def _last_content(messages: list[dict]) -> str:
    if not messages:
        return ""
    last = messages[-1] if isinstance(messages[-1], dict) else {}
    c = last.get("content", "")
    if isinstance(c, list):
        c = " ".join(p.get("text", "") for p in c if isinstance(p, dict))
    return str(c or "")


def is_owui_task(messages: list[dict]) -> bool:
    """True NUR bei OWUI-HINTERGRUNDtasks (Titel-/Tag-/Query-/Follow-up-/Autocomplete-
    Generierung): '### Task:'-Prompt OHNE eingebetteten Dokument-Kontext. Die RAG-
    Antwort-Generierung (mit <context>/<source resource-id=...>) ist die echte Frage und
    MUSS durch die volle Pipeline (dort holt read_full_document den Volltext)."""
    c = _last_content(messages)
    return bool(_OWUI_TASK_RE.match(c)) and not _OWUI_CTX_RE.search(c)


def owui_real_query(content: str) -> str:
    """Aus OWUIs RAG-Template die echte <user_query> herausziehen (sonst unveraendert),
    damit die Pipeline auf der Nutzerfrage statt auf dem Template-Boilerplate arbeitet."""
    m = _OWUI_USERQ_RE.search(content or "")
    return m.group(1).strip() if m else (content or "")


async def simple_completion(messages: list[dict], temperature: float = 0.3,
                            max_tokens: int = 1024) -> str:
    """Genau EIN LLM-Durchlauf ohne Agent-Pipeline (kein RAG/Web/Code/Critic).
    Fuer OWUI-Hintergrundtasks: die Task-Nachrichten enthalten ihr Ausgabeformat
    (z.B. JSON fuer die Query-Generierung) bereits selbst -> 1:1 ans Hauptmodell."""
    payload = {"model": LLM_MODEL, "messages": messages,
               "temperature": temperature, "max_tokens": max_tokens, "stream": False}
    try:
        async with httpx.AsyncClient() as http:
            r = await http.post(LLM_BASE_URL.rstrip("/") + "/chat/completions",
                                json=payload,
                                headers={"Authorization": f"Bearer {LLM_API_KEY}"},
                                timeout=60.0)
            r.raise_for_status()
            data = r.json()
            return ((data.get("choices") or [{}])[0].get("message") or {}).get("content", "") or ""
    except Exception as e:
        log.warning("OWUI-Task-Passthrough (simple_completion) fehlgeschlagen: %s", e)
        return ""


# --- Tool-Funktionen (schlicht, von beiden Varianten genutzt) ---------------
def _doc_name_key(s: str) -> str:
    """Dateiname -> normalisierter Schluessel (klein, ohne Pfad/Endung/Sonderzeichen)."""
    s = (s or "").lower().rsplit("/", 1)[-1]
    s = re.sub(r"\.[a-z0-9]{1,5}$", "", s)   # Endung weg
    return re.sub(r"[^a-z0-9]+", "", s)        # nur alphanumerisch


def _doc_matches(chunk_doc: str, attached: str) -> bool:
    """Gehoert ein RAG-/Morphik-Chunk zur angehaengten Datei? (robuster Namensabgleich)."""
    a, b = _doc_name_key(chunk_doc), _doc_name_key(attached)
    if not a or not b:
        return False
    return a == b or a in b or b in a or a[:18] == b[:18]


async def t_retrieve_documents(http: httpx.AsyncClient, query: str,
                               only_doc: str | None = None) -> str:
    """RAGFlow-Retrieval. [VERIFY] Endpoint/Payload je RAGFlow-Version.
    only_doc: nur Chunks DIESES Dokuments (Chat-Upload-Bezug -> keine Fremd-Doku-
    Kontamination; ist es noch nicht indiziert, kommt eine klare Notiz statt Fremdtreffer)."""
    if not RAGFLOW_API_URL or not RAGFLOW_API_KEY:
        return "Kein RAGFlow konfiguriert (RAGFLOW_API_KEY fehlt)."
    url = f"{RAGFLOW_API_URL}/api/v1/retrieval"
    payload = {"question": query, "dataset_ids": RAGFLOW_DATASET_IDS,
               "page_size": 20 if only_doc else 8}
    headers = {"Authorization": f"Bearer {RAGFLOW_API_KEY}"}
    try:
        r = await http.post(url, json=payload, headers=headers, timeout=30.0)
        r.raise_for_status()
        data = r.json()
        chunks = (data.get("data") or {}).get("chunks") or data.get("chunks") or []
        if not chunks:
            return "Keine relevanten Dokumente gefunden."
        out = []
        for c in chunks:
            content = c.get("content") or c.get("content_with_weight") or ""
            doc = c.get("document_keyword") or c.get("docnm_kwd") or "?"
            if only_doc and not _doc_matches(doc, only_doc):
                continue
            out.append(f"[{doc}] {content}")
            if len(out) >= 8:
                break
        if not out:
            if only_doc:
                return (f"('{only_doc}' noch nicht im RAGFlow-Index (Ingest evtl. noch am Laufen) "
                        f"-> Antwort stuetzt sich auf den Volltext.)")
            return "Keine relevanten Dokumente gefunden."
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


async def t_retrieve_multimodal(http: httpx.AsyncClient, query: str,
                                only_doc: str | None = None) -> str:
    """Morphik-Retrieval fuer bild-/tabellenlastige Dokumente (optional).
    only_doc: nur Chunks dieser Datei (Chat-Upload-Bezug, keine Fremd-Doku-Kontamination)."""
    if not MORPHIK_API_URL:
        return "Morphik nicht konfiguriert."
    # [VERIFY] Morphik-Endpoint/Payload gegen deine Version pruefen.
    url = f"{MORPHIK_API_URL}/retrieve/chunks"
    headers = morphik_auth_header()
    try:
        r = await http.post(url, json={"query": query, "k": 12 if only_doc else 6},
                            headers=headers, timeout=40.0)
        r.raise_for_status()
        data = r.json()
        chunks = data if isinstance(data, list) else (data.get("chunks") or data.get("results") or [])
        if not (isinstance(chunks, list) and chunks):
            return "Keine multimodalen Treffer."
        out = []
        for c in chunks:
            if isinstance(c, dict):
                meta = c.get("metadata") or {}
                src = (meta.get("original_filename") or meta.get("filename")
                       or c.get("filename") or "")
                if only_doc and not (src and _doc_matches(src, only_doc)):
                    continue  # ohne sicheren Datei-Bezug im scoped-Modus auslassen
                out.append(str(c.get("content", c))[:800])
            else:
                if only_doc:
                    continue
                out.append(str(c)[:800])
            if len(out) >= 6:
                break
        if not out:
            return (f"('{only_doc}' nicht in Morphik.)" if only_doc else "Keine multimodalen Treffer.")
        return "\n\n".join(out)[:6000]
    except Exception as e:
        log.warning("Morphik-Retrieval fehlgeschlagen (ignoriert): %s", e)
        return "Keine multimodalen Treffer."


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


# --- Volltext: OWUI-Upload lesen + zu Text parsen ---------------------------
_OWUI_PREFIX = "/app/backend/data"  # OWUI-interner Datenpfad (im Volume)


def _owui_file_refs(body: dict) -> list:
    """Datei-Referenzen aus dem OWUI-Request einsammeln (mehrere Formen)."""
    if not isinstance(body, dict):
        return []
    cand = list(body.get("files") or [])
    cand += (body.get("metadata") or {}).get("files") or []
    for m in body.get("messages") or []:
        if isinstance(m, dict):
            cand += m.get("files") or []
    refs, seen = [], set()
    for f in cand:
        if not isinstance(f, dict):
            continue
        inner = f.get("file") if isinstance(f.get("file"), dict) else f
        fid = inner.get("id") or f.get("id")
        if not fid or fid in seen:
            continue
        seen.add(fid)
        meta = inner.get("meta") or {}
        refs.append({"id": fid,
                     "name": inner.get("filename") or inner.get("name") or meta.get("name") or fid,
                     "path": inner.get("path") or f.get("path"),
                     "content_type": meta.get("content_type") or inner.get("content_type") or ""})
    # Fallback: File-ID aus dem RAG-Kontext der Nachrichten (<source ... resource-id="...">).
    for m in body.get("messages") or []:
        c = m.get("content") if isinstance(m, dict) else None
        for rid in re.findall(r'resource-id="([0-9a-fA-F-]{8,})"', str(c or "")):
            if rid not in seen:
                seen.add(rid)
                refs.append({"id": rid, "name": "", "path": None, "content_type": ""})
    return refs


def _owui_local_path(ref: dict):
    """OWUI-Pfad auf das gemountete Volume mappen; sonst per ID im uploads/-Ordner."""
    p = ref.get("path")
    if isinstance(p, str) and p:
        if p.startswith(_OWUI_PREFIX):
            cand = OWUI_DATA_DIR.rstrip("/") + p[len(_OWUI_PREFIX):]
            if os.path.exists(cand):
                return cand
        if os.path.exists(p):
            return p
    for c in glob.glob(f"{OWUI_DATA_DIR.rstrip('/')}/uploads/{ref['id']}_*"):
        return c
    return None


def _strip_html(s: str) -> str:
    s = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", s)
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    return re.sub(r"\n{3,}", "\n\n", html.unescape(s))


def file_to_text(name: str, ctype: str, data: bytes) -> str:
    """Datei -> Text. epub/docx/html/txt/md/csv/json via Standardbibliothek, pdf via pypdf."""
    n = (name or "").lower(); ct = (ctype or "").lower()
    try:
        if n.endswith(".epub") or "epub" in ct:
            parts = []
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                for zi in z.namelist():
                    if zi.lower().endswith((".xhtml", ".html", ".htm")):
                        parts.append(_strip_html(z.read(zi).decode("utf-8", "ignore")))
            return "\n\n".join(parts)
        if n.endswith(".docx") or "wordprocessingml" in ct:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                xml = z.read("word/document.xml").decode("utf-8", "ignore")
            return _strip_html(xml.replace("</w:p>", "\n"))
        if n.endswith(".pdf") or "pdf" in ct:
            try:
                import pypdf
                rd = pypdf.PdfReader(io.BytesIO(data))
                return "\n".join((pg.extract_text() or "") for pg in rd.pages)
            except Exception as e:
                return f"(PDF nicht lesbar: {e})"
        if n.endswith((".html", ".htm")) or "html" in ct:
            return _strip_html(data.decode("utf-8", "ignore"))
        return data.decode("utf-8", "replace")  # txt/md/csv/json/code/...
    except Exception as e:
        return f"(Datei nicht parsbar: {e})"


_OWUI_ID_PREFIX_RE = re.compile(r"^[0-9a-fA-F-]{8,}_(.+)$")  # '{id}_{originalname}'


def _strip_owui_id_prefix(name: str) -> str:
    m = _OWUI_ID_PREFIX_RE.match(name or "")
    return m.group(1) if m else (name or "")


def _recent_upload(max_age_s: int = None):
    """Pfad des JUENGSTEN Uploads im OWUI-Volume (oder None). Fallback, wenn OWUI dem
    externen Modell KEINE Datei-Referenz mitschickt (beobachtet bei research-agent):
    direkt nach dem Upload fragt der User -> die frischeste Datei ist die gemeinte.
    Zeitfenster (Default 15 min) verhindert, dass alte Uploads faelschlich gezogen werden."""
    if max_age_s is None:
        max_age_s = RECENT_UPLOAD_MAX_AGE_S
    try:
        cands = glob.glob(f"{OWUI_DATA_DIR.rstrip('/')}/uploads/*")
    except Exception:
        return None
    newest, newest_mt = None, 0.0
    for p in cands:
        if not os.path.isfile(p):
            continue
        try:
            mt = os.path.getmtime(p)
        except Exception:
            continue
        if mt > newest_mt:
            newest, newest_mt = p, mt
    if not newest:
        return None
    age = time.time() - newest_mt
    if age > max_age_s:
        log.info("Volltext: juengster Upload zu alt (%.0fs > %ds): %s",
                 age, max_age_s, os.path.basename(newest))
        return None
    return newest


def read_full_document(body: dict):
    """(name, text) der GROESSTEN lesbaren angehaengten Datei oder None. Liest direkt
    aus dem gemounteten OWUI-Upload-Volume (kein OWUI-API, kein 401). Faellt auf den
    JUENGSTEN Upload zurueck, wenn OWUI keine verwertbare Datei-Referenz mitschickt."""
    refs = _owui_file_refs(body)
    best = None  # (size, name, content_type, local_path)
    for ref in refs:
        lp = _owui_local_path(ref)
        if not lp:
            log.info("Volltext: Datei NICHT im Volume gefunden (id=%s path=%s dir=%s)",
                     ref.get("id"), ref.get("path"), OWUI_DATA_DIR)
            continue
        try:
            sz = os.path.getsize(lp)
        except Exception:
            continue
        if sz > FULLDOC_MAX_BYTES:
            log.warning("Volltext: %s zu gross (%d B) -> uebersprungen", ref["name"], sz)
            continue
        name = ref["name"] or os.path.basename(lp)
        if ref["id"] and name.startswith(ref["id"] + "_"):  # OWUI-Praefix {id}_ entfernen
            name = name[len(ref["id"]) + 1:]
        if best is None or sz > best[0]:
            best = (sz, name, ref["content_type"], lp)

    if best is None:
        # Keine verwertbare Datei-Referenz -> juengsten Upload aus dem Volume nehmen.
        lp = _recent_upload()
        if not lp:
            log.info("Volltext: KEINE Datei-Referenz im Request UND kein frischer Upload "
                     "im Volume (body-keys=%s)",
                     list(body.keys()) if isinstance(body, dict) else type(body).__name__)
            return None
        try:
            sz = os.path.getsize(lp)
        except Exception:
            return None
        if sz > FULLDOC_MAX_BYTES:
            log.warning("Volltext: juengster Upload zu gross (%d B) -> uebersprungen", sz)
            return None
        name = _strip_owui_id_prefix(os.path.basename(lp))
        log.info("Volltext: nutze juengsten Upload als Fallback: %s", name)
        best = (sz, name, "", lp)

    _, name, ctype, lp = best
    try:
        with open(lp, "rb") as fh:
            data = fh.read()
    except Exception as e:
        log.warning("Volltext: %s nicht lesbar: %s", lp, e)
        return None
    text = file_to_text(name, ctype, data)
    if not text or not text.strip():
        return None
    log.info("Volltext geladen: %s (%d Zeichen)", name, len(text))
    return name, text


# --- Eigennamen-Kandidaten datengetrieben aus dem Volltext -------------------
# Damit das Modell bei "zaehle Namen/Orte" NICHT aus dem Gedaechtnis raet (falsche
# Schreibweisen -> 0-Treffer; z.B. engl. 'Isengard' statt dt. 'Isengart'), liefern
# wir die TATSAECHLICH im Text vorkommenden, exakt geschriebenen Tokens mit Anzahl.
# Heuristik: grossgeschriebene Wort-Tokens zaehlen; Satzanfangs-Funktionswoerter
# (der/Der, und/Und ...) raus, indem die KLEIN-Variante haeufiger sein muss als die
# Gross-Variante. Deutsche Nomen bleiben drin (immer gross) -> das Modell filtert
# Personen/Orte mit Weltwissen aus den ECHTEN Tokens (richtige Schreibweise).
_WORD_RE = re.compile(r"[^\W\d_]{2,}", re.UNICODE)  # >=2 Unicode-Buchstaben, keine Ziffern
# Kompakte Funktionswort-Stoppliste (Artikel/Pronomen/Konjunktionen/Praepositionen/
# Hilfsverben) — falls eine Form am Satzanfang doch ueberwiegt (kurze Dokumente). Echte
# Nomen/Eigennamen bleiben drin; Personen/Orte filtert das Modell aus den Tokens.
_STOP_DE = frozenset("""der die das den dem des ein eine einen einem eines und oder aber
doch denn sondern sie er es ich du wir ihr man mir mich dir dich ihm ihn ihnen uns euch
als wenn weil dass da wie was wer wo wann warum welche welcher welches so nun dann also
auch nur noch schon sehr hier dort jetzt im in an auf aus bei mit nach von vor zu zum zur
ueber unter durch fuer ohne gegen um bis seit waehrend nicht kein keine mein meine dein
diese dieser dieses jener jede jeder alle alles einige viele manche solche hatte hatten
hat war waren ist sind wird werden wurde wurden kann konnte muss musste will wollte soll
sollte habe haben sich sein seine ihre dass""".split())


def proper_noun_candidates(text: str, n: int = 80, min_count: int = 3):
    """[(token, count), ...] der haeufigsten ueberwiegend grossgeschriebenen Tokens
    (exakte Schreibweise wie im Text), nach Haeufigkeit sortiert."""
    cap: "collections.Counter[str]" = collections.Counter()
    low: "collections.Counter[str]" = collections.Counter()
    for m in _WORD_RE.finditer(text or ""):
        w = m.group(0)
        if w[:1].isupper():
            cap[w] += 1
        else:
            low[w.lower()] += 1
    out = []
    for w, c in cap.most_common():
        if c < min_count:
            break
        wl = w.lower()
        if wl in _STOP_DE:              # Funktionswort -> raus
            continue
        if low.get(wl, 0) > c:          # Satzanfangs-Wort (klein dominanter) -> raus
            continue
        out.append((w, c))
        if len(out) >= n:
            break
    return out


# --- Auto-Ingest: Chat-Upload -> ingest-router (RAGFlow/Morphik) -------------
async def _ingest_one_upload(name: str, ctype: str, lp: str) -> None:
    try:
        with open(lp, "rb") as fh:
            data = fh.read()
        async with httpx.AsyncClient() as http:
            r = await http.post(INGEST_ROUTER_URL + "/ingest",
                                files={"file": (name, data, ctype or "application/octet-stream")},
                                timeout=180.0)
        if r.status_code == 200:
            try:
                tgt = r.json().get("target", "?")
            except Exception:
                tgt = "?"
            log.info("Auto-Ingest: '%s' -> %s", name, tgt)
        else:
            log.warning("Auto-Ingest '%s': HTTP %s (%s)", name, r.status_code, r.text[:200])
    except Exception as e:
        log.warning("Auto-Ingest '%s' fehlgeschlagen (ignoriert): %s", name, e)


def schedule_ingest(body: dict) -> None:
    """Frische Chat-Uploads idempotent + NICHT-blockierend an den ingest-router schicken
    (klassifiziert -> RAGFlow/Morphik). Rein lokal (kein Egress). Ersetzt den OWUI-Filter:
    der Agent kennt die Datei ueber die resource-id/den lokalen Pfad bereits."""
    if not (AGENT_AUTO_INGEST and INGEST_ROUTER_URL):
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    for ref in _owui_file_refs(body):
        key = ref.get("id") or ref.get("path") or ""
        if not key or key in _INGESTED:
            continue
        lp = _owui_local_path(ref)
        if not lp:
            continue
        _INGESTED.add(key)
        name = ref.get("name") or os.path.basename(lp)
        if ref.get("id") and name.startswith(ref["id"] + "_"):
            name = name[len(ref["id"]) + 1:]
        t = loop.create_task(_ingest_one_upload(_strip_owui_id_prefix(name),
                                                ref.get("content_type"), lp))
        _INGEST_TASKS.add(t)
        t.add_done_callback(_INGEST_TASKS.discard)


# --- Code-Sandbox -----------------------------------------------------------
async def _post_run(http: httpx.AsyncClient, url: str, code: str, files: dict | None = None) -> dict:
    payload = {"code": code}
    if files:
        payload["files"] = files
    r = await http.post(url, json=payload, timeout=180.0)
    r.raise_for_status()
    return r.json()


_TEXT_OUT = (".csv", ".tsv", ".txt", ".md", ".json", ".yaml", ".yml", ".log",
             ".py", ".xml", ".html", ".ini")


async def t_run_code(http: httpx.AsyncClient, code: str, files: dict | None = None) -> str:
    """Python ausfuehren. Bei Eingabedateien (Volltext-Modus) direkt die luftdichte
    Subprozess-Sandbox; sonst bevorzugt die microVM. Erzeugte Text-Dateien (CSV etc.)
    werden INLINE zurueckgegeben (kopierbar)."""
    engine = "microVM (Microsandbox)"
    res = None
    if MSB_EXECUTOR_URL and not files:  # microVM-Pfad nur ohne Eingabedateien
        try:
            res = await _post_run(http, MSB_EXECUTOR_URL, code)
            if res.get("returncode") == -1 and "SDK nicht installiert" in (res.get("stderr") or ""):
                res = None  # Executor laeuft, aber SDK fehlt -> Fallback
        except Exception:
            log.warning("microVM-Executor nicht erreichbar -> Fallback auf Subprozess-Sandbox")
            res = None
    if res is None:
        engine = "Subprozess-Sandbox" + (" (Volltext)" if files else " (FALLBACK, kein microVM!)")
        try:
            res = await _post_run(http, SANDBOX_RUN_URL, code, files)
        except Exception as e:
            log.exception("Code-Ausfuehrung fehlgeschlagen")
            return f"Sandbox-Fehler: {e}"

    out = [f"[Engine: {engine}] returncode={res.get('returncode')}",
           f"--- stdout ---\n{res.get('stdout','')}",
           f"--- stderr ---\n{res.get('stderr','')}"]
    fl = res.get("files", [])
    if not fl:
        out.append("--- Dateien --- keine")
    for f in fl:
        nm, sz, b64 = f.get("name", ""), f.get("size", 0), f.get("base64")
        if b64 and nm.lower().endswith(_TEXT_OUT) and sz <= 100_000:
            try:
                content = base64.b64decode(b64).decode("utf-8", "replace")
            except Exception:
                content = "(nicht dekodierbar)"
            out.append(f"--- Datei: {nm} ({sz} B) ---\n{content[:12000]}")
        else:
            out.append(f"--- Datei: {nm} ({sz} B) --- (binaer/zu gross; im Sandbox-Volume)")
    return "\n".join(out)
