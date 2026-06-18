"""
PII-Masking-Such-Proxy (Inklusive Burst-Bremse)
==============================================
Sitzt VOR SearXNG. Jede eingehende Suchanfrage wird mit Microsoft Presidio
auf personenbezogene Daten gescannt (Namen, E-Mail, Telefon, IBAN, Kreditkarte,
Standort, ...) und ZUSAETZLICH per Custom-Recognizer auf oesterreichische VSNR
(Versicherungsnummer, 10-stellig) geprueft. Treffer werden ersetzt, BEVOR die
Anfrage SearXNG / das Web erreicht. So koennen weder OWUI noch der Agent
versehentlich PII ueber die Websuche nach draussen leaken.

ERWEITERUNG:
Ein globaler asynchroner Lock fängt parallele "Maschinengewehr"-Bursts von
Open WebUI ab und verarbeitet Suchanfragen strikt nacheinander (sequentiell)
mit einer zufälligen menschlichen Pause von 4 bis 7 Sekunden. Das verhindert
effektiv CAPTCHAs und HTTP 429er Blocks bei globalen Suchmaschinen.

Endpoint (kompatibel zu OWUI SEARXNG_QUERY_URL und zum Agent-Such-Tool):
    GET /search?q=<query>&format=json   ->  leitet maskiert an SearXNG weiter
"""

import asyncio
import logging
import os
import random
import time

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from presidio_analyzer import AnalyzerEngine, Pattern, PatternRecognizer
from presidio_analyzer.nlp_engine import NlpEngineProvider
from presidio_anonymizer import AnonymizerEngine
from presidio_anonymizer.entities import OperatorConfig

# Log-Level per ENV steuerbar (Default INFO). Im Stack auf DEBUG gesetzt
# (PRESIDIO_LOG_LEVEL in der .env / docker-compose) -> volle Websuche-Diagnostik.
_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=getattr(logging, _LEVEL, logging.INFO))
log = logging.getLogger("presidio-proxy")

SEARXNG_URL = os.environ.get("SEARXNG_URL", "http://searxng:8080").rstrip("/")
LANG = os.environ.get("PROXY_LANG", "de")

# --- Such-Backend (Default: SearXNG; optional externe Such-APIs) -------------
# Wird die Server-IP von Engines "soft-geblockt" (HTTP 200 + 0 Treffer), liefern
# offizielle Such-APIs IP-UNABHAENGIG Ergebnisse. Die PII-MASKIERUNG bleibt voll
# erhalten: es wird IMMER zuerst maskiert und DANN erst an die API geschickt.
#   SEARCH_BACKEND = searxng (Default) | brave | tavily | serper | google_pse
SEARCH_BACKEND = os.environ.get("SEARCH_BACKEND", "searxng").strip().lower()
SEARCH_RESULT_COUNT = int(os.environ.get("SEARCH_RESULT_COUNT", "10") or "10")
BRAVE_API_KEY = os.environ.get("BRAVE_API_KEY", "")
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")
SERPER_API_KEY = os.environ.get("SERPER_API_KEY", "")
GOOGLE_PSE_API_KEY = os.environ.get("GOOGLE_PSE_API_KEY", "")
GOOGLE_PSE_ENGINE_ID = os.environ.get("GOOGLE_PSE_ENGINE_ID", "")

# --- Anti-Flood-Drossel ------------------------------------------------------
# Serialisiert ALLE Suchen (genau EIN Call gleichzeitig, via search_lock) und
# haelt zwischen aufeinanderfolgenden Calls eine variable Pause ein. Schuetzt die
# SearXNG-Engines UND externe Such-APIs (z.B. Brave-Free = 1 req/s) davor, von
# parallelen OWUI-Suchbursts geflutet zu werden. Werte per ENV justierbar.
SEARCH_THROTTLE = os.environ.get("SEARCH_THROTTLE", "true").strip().lower() in (
    "1", "true", "yes", "on",
)
SEARCH_MIN_PAUSE = float(os.environ.get("SEARCH_MIN_PAUSE", "5") or "5")
SEARCH_MAX_PAUSE = float(os.environ.get("SEARCH_MAX_PAUSE", "10") or "10")

app = FastAPI(title="PII-Masking Search Proxy")

# --- Globale Bremse für Parallel-Anfragen (Throttling Queue) ----------------
# Dieser Lock sorgt dafür, dass im async-Event-Loop IMMER nur eine Suche läuft.
# (uvicorn laeuft mit 1 Worker -> der Lock ist prozessweit wirksam.)
search_lock = asyncio.Lock()
_last_call = 0.0  # monotonic-Zeit des letzten Such-Calls (fuer den Mindestabstand)

# --- Presidio Engines (einmalig laden) --------------------------------------
# NLP-Engine fuer DE + EN konfigurieren (sonst kennt der Analyzer nur 'en').
_nlp_conf = {
    "nlp_engine_name": "spacy",
    "models": [
        {"lang_code": "de", "model_name": "de_core_news_lg"},
        {"lang_code": "en", "model_name": "en_core_web_lg"},
    ],
}
_provider = NlpEngineProvider(nlp_configuration=_nlp_conf)
_nlp_engine = _provider.create_engine()
analyzer = AnalyzerEngine(nlp_engine=_nlp_engine, supported_languages=["de", "en"])
anonymizer = AnonymizerEngine()

# Custom-Recognizer: oesterreichische Sozialversicherungsnummer (VSNR), 10-stellig.
# (Heuristik: 4 Ziffern Laufnummer + 6 Ziffern Geburtsdatum.)
vsnr_pattern = Pattern(name="vsnr_10", regex=r"\b\d{4}\s?\d{2}\d{2}\d{2}\b", score=0.85)
vsnr_recognizer = PatternRecognizer(
    supported_entity="AT_VSNR",
    patterns=[vsnr_pattern],
    supported_language=LANG,
)
analyzer.registry.add_recognizer(vsnr_recognizer)

# Welche Entitaeten maskiert werden und womit:
OPERATORS = {
    "DEFAULT": OperatorConfig("replace", {"new_value": "<PII>"}),
    "AT_VSNR": OperatorConfig("replace", {"new_value": "<VSNR>"}),
    "PERSON": OperatorConfig("replace", {"new_value": "<NAME>"}),
    "EMAIL_ADDRESS": OperatorConfig("replace", {"new_value": "<EMAIL>"}),
    "PHONE_NUMBER": OperatorConfig("replace", {"new_value": "<TEL>"}),
    "IBAN_CODE": OperatorConfig("replace", {"new_value": "<IBAN>"}),
    "CREDIT_CARD": OperatorConfig("replace", {"new_value": "<CC>"}),
    "LOCATION": OperatorConfig("replace", {"new_value": "<ORT>"}),
}


def mask(text: str) -> str:
    if not text:
        return text
    try:
        results = analyzer.analyze(text=text, language=LANG)
        if not results:
            return text
        masked = anonymizer.anonymize(
            text=text, analyzer_results=results, operators=OPERATORS
        )
        if masked.text != text:
            log.info("PII maskiert: %d Treffer", len(results))
        return masked.text
    except Exception:
        # Im Zweifel lieber HART blocken als ungefiltert durchlassen.
        log.exception("Maskierung fehlgeschlagen -> Anfrage geblockt")
        return "<BLOCKED>"


async def _api_search(q: str) -> list[dict]:
    """Offizielle Such-API abfragen und auf die SearXNG-JSON-Form normalisieren
    ([{"url","title","content"}]). 'q' ist bereits PII-maskiert. So bekommt OWUI
    exakt dieselbe Struktur wie von SearXNG -> kein OWUI-Umbau noetig."""
    n = SEARCH_RESULT_COUNT
    async with httpx.AsyncClient(timeout=20.0) as client:
        if SEARCH_BACKEND == "tavily":
            if not TAVILY_API_KEY:
                raise RuntimeError("SEARCH_BACKEND=tavily, aber TAVILY_API_KEY fehlt")
            r = await client.post("https://api.tavily.com/search", json={
                "api_key": TAVILY_API_KEY, "query": q,
                "max_results": n, "search_depth": "basic"})
            r.raise_for_status()
            return [{"url": x.get("url"), "title": x.get("title"),
                     "content": x.get("content", "")}
                    for x in r.json().get("results", []) if x.get("url")]
        if SEARCH_BACKEND == "brave":
            if not BRAVE_API_KEY:
                raise RuntimeError("SEARCH_BACKEND=brave, aber BRAVE_API_KEY fehlt")
            r = await client.get(
                "https://api.search.brave.com/res/v1/web/search",
                params={"q": q, "count": n},
                headers={"X-Subscription-Token": BRAVE_API_KEY,
                         "Accept": "application/json"})
            r.raise_for_status()
            web = r.json().get("web") or {}
            return [{"url": x.get("url"), "title": x.get("title"),
                     "content": x.get("description", "")}
                    for x in web.get("results", []) if x.get("url")]
        if SEARCH_BACKEND == "serper":
            if not SERPER_API_KEY:
                raise RuntimeError("SEARCH_BACKEND=serper, aber SERPER_API_KEY fehlt")
            r = await client.post(
                "https://google.serper.dev/search",
                headers={"X-API-KEY": SERPER_API_KEY,
                         "Content-Type": "application/json"},
                json={"q": q, "num": n})
            r.raise_for_status()
            return [{"url": x.get("link"), "title": x.get("title"),
                     "content": x.get("snippet", "")}
                    for x in r.json().get("organic", []) if x.get("link")]
        if SEARCH_BACKEND == "google_pse":
            if not (GOOGLE_PSE_API_KEY and GOOGLE_PSE_ENGINE_ID):
                raise RuntimeError("SEARCH_BACKEND=google_pse, aber GOOGLE_PSE_API_KEY/"
                                   "GOOGLE_PSE_ENGINE_ID fehlt")
            r = await client.get(
                "https://www.googleapis.com/customsearch/v1",
                params={"key": GOOGLE_PSE_API_KEY, "cx": GOOGLE_PSE_ENGINE_ID,
                        "q": q, "num": min(n, 10)})
            r.raise_for_status()
            return [{"url": x.get("link"), "title": x.get("title"),
                     "content": x.get("snippet", "")}
                    for x in r.json().get("items", []) if x.get("link")]
        raise RuntimeError(f"Unbekanntes SEARCH_BACKEND: '{SEARCH_BACKEND}' "
                           "(erlaubt: searxng|brave|tavily|serper|google_pse)")


@app.get("/healthz")
async def healthz():
    return {"ok": True, "backend": SEARCH_BACKEND}


@app.get("/search")
async def search(request: Request):
    params = dict(request.query_params)
    raw_q = params.get("q", "")

    # 1. PII-Maskierung IMMER zuerst (gilt fuer SearXNG UND API-Backends).
    params["q"] = mask(raw_q)
    # JSON erzwingen, damit Agent/OWUI strukturiert parsen koennen:
    params.setdefault("format", "json")
    masked_q = params["q"]

    log.info("[Queue] Suchanfrage eingereiht: '%s'", masked_q)

    # 2. Anti-Flood-Drossel fuer ALLE Backends: der search_lock laesst genau EINEN
    #    Call gleichzeitig durch; davor wird auf SEARCH_MIN..MAX_PAUSE Abstand zum
    #    vorherigen Call-START aufgefuellt (erster Call wartet nicht). So sieht jede
    #    Engine/API nur sauber sequentielle Einzelanfragen statt paralleler Bursts.
    global _last_call
    async with search_lock:
        if SEARCH_THROTTLE:
            gap = random.uniform(SEARCH_MIN_PAUSE, SEARCH_MAX_PAUSE)
            wait = _last_call + gap - time.monotonic()
            if wait > 0:
                log.info("[Queue] Anti-Flood: %.2fs Pause vor '%s'", wait, masked_q)
                await asyncio.sleep(wait)
        _last_call = time.monotonic()

        # 2a. API-Backend (Brave/Tavily/Serper/Google-PSE) -> normalisiertes JSON.
        if SEARCH_BACKEND != "searxng":
            try:
                results = await _api_search(masked_q)
                log.info("[API:%s] %d Treffer fuer '%s'", SEARCH_BACKEND, len(results), masked_q)
                if not results:
                    log.warning("[API:%s] 0 Treffer -> API-Key/Kontingent/Query pruefen.", SEARCH_BACKEND)
                return JSONResponse({"results": results,
                                     "number_of_results": len(results),
                                     "query": masked_q})
            except httpx.HTTPStatusError as e:
                code = e.response.status_code if e.response is not None else "?"
                body = e.response.text[:300] if e.response is not None else ""
                log.error("[API:%s] HTTP %s: %s", SEARCH_BACKEND, code, body)
                return JSONResponse({"error": f"{SEARCH_BACKEND}: HTTP {code}", "results": []},
                                    status_code=502)
            except Exception as e:
                log.exception("[API:%s] Suche fehlgeschlagen", SEARCH_BACKEND)
                return JSONResponse({"error": str(e), "results": []}, status_code=502)

        # 2b. SearXNG-Backend (Default): X-Forwarded-For gegen Bot-Warnungen.
        fwd_headers = {"X-Forwarded-For": "127.0.0.1", "X-Real-IP": "127.0.0.1"}

        # 3. Request an SearXNG absenden
        try:
            log.info("[Queue] Sende Anfrage an SearXNG: '%s'", params["q"])
            async with httpx.AsyncClient(
                timeout=25.0
            ) as client:  # Timeout minimal erhöht, da Deep Research Zeit braucht
                r = await client.get(
                    f"{SEARXNG_URL}/search", params=params, headers=fwd_headers
                )

            ctype = r.headers.get("content-type", "")
            if "application/json" in ctype:
                data = r.json()
                # WICHTIG fuer das Websuche-Debugging: zeigt schwarz auf weiss, ob
                # SearXNG ueberhaupt Treffer liefert. n=0 ist die haeufigste Ursache
                # fuer OWUIs "404: No results found from web search" (Engines leer/
                # rate-limitiert) -> in Grafana/Loki sofort sichtbar, WO es klemmt.
                n = len(data.get("results", [])) if isinstance(data, dict) else 0
                log.info(
                    "[Queue] SearXNG-Antwort: HTTP %s, %d Treffer fuer '%s'",
                    r.status_code, n, params["q"],
                )
                if n == 0 and isinstance(data, dict):
                    # Engine-Diagnostik aus der SearXNG-JSON, falls vorhanden:
                    unresp = data.get("unresponsive_engines") or []
                    if unresp:
                        log.warning(
                            "[Queue] SearXNG: 0 Treffer + ausgefallene Engines: %s",
                            unresp,
                        )
                    else:
                        log.warning(
                            "[Queue] SearXNG: 0 Treffer (Engines ohne Fehler -> "
                            "Query evtl. zu spezifisch/maskiert oder Engines leer).",
                        )
                return JSONResponse(content=data, status_code=r.status_code)
            # Kein JSON -> meist HTML (CAPTCHA/Block-Seite). Auch das ist Diagnostik.
            log.warning(
                "[Queue] SearXNG-Antwort NICHT JSON (content-type=%s, HTTP %s) -> "
                "evtl. CAPTCHA/Block-Seite statt Ergebnissen.",
                ctype or "(leer)", r.status_code,
            )
            return PlainTextResponse(
                content=r.text,
                status_code=r.status_code,
                media_type=ctype or "text/plain",
            )

        except Exception as e:
            log.exception("SearXNG-Weiterleitung fehlgeschlagen")
            return JSONResponse(
                content={"error": str(e), "results": []}, status_code=502
            )
