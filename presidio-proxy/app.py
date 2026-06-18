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

app = FastAPI(title="PII-Masking Search Proxy")

# --- Globale Bremse für Parallel-Anfragen (Throttling Queue) ----------------
# Dieser Lock sorgt dafür, dass im async-Event-Loop IMMER nur eine Suche läuft.
search_lock = asyncio.Lock()

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


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/search")
async def search(request: Request):
    params = dict(request.query_params)
    raw_q = params.get("q", "")

    # 1. PII Maskierung durchführen
    params["q"] = mask(raw_q)
    # JSON erzwingen, damit Agent/OWUI strukturiert parsen koennen:
    params.setdefault("format", "json")

    # X-Forwarded-For setzen gegen SearXNG Bot-Warnungen
    fwd_headers = {"X-Forwarded-For": "127.0.0.1", "X-Real-IP": "127.0.0.1"}

    log.info("[Queue] Suchanfrage eingereiht: '%s'", params["q"])

    # 2. Warteschlange und künstliche Verzögerung aktivieren
    async with search_lock:
        # Erzeuge eine variable Pause zwischen 4.0 und 7.0 Sekunden
        cooldown = random.uniform(4.0, 7.0)
        log.info(
            "[Queue] Slot belegt. Erzwungene Atempause für %s Sekunden zur Bot-Vermeidung...",
            f"{cooldown:.2f}",
        )
        await asyncio.sleep(cooldown)

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
