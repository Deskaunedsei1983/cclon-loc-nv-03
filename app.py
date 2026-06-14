"""
PII-Masking-Such-Proxy
======================
Sitzt VOR SearXNG. Jede eingehende Suchanfrage wird mit Microsoft Presidio
auf personenbezogene Daten gescannt (Namen, E-Mail, Telefon, IBAN, Kreditkarte,
Standort, ...) und ZUSAETZLICH per Custom-Recognizer auf oesterreichische VSNR
(Versicherungsnummer, 10-stellig) geprueft. Treffer werden ersetzt, BEVOR die
Anfrage SearXNG / das Web erreicht. So koennen weder OWUI noch der Agent
versehentlich PII ueber die Websuche nach draussen leaken.

Endpoint (kompatibel zu OWUI SEARXNG_QUERY_URL und zum Agent-Such-Tool):
    GET /search?q=<query>&format=json   ->  leitet maskiert an SearXNG weiter

Hinweis: Presidio ist sehr gut, aber kein 100%-Garant. Fuer maximale Sicherheit
zusaetzlich einen Allowlist-Ansatz fahren oder die Websuche ganz deaktivieren.
"""

import os
import logging

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from presidio_analyzer import AnalyzerEngine, PatternRecognizer, Pattern
from presidio_analyzer.nlp_engine import NlpEngineProvider
from presidio_anonymizer import AnonymizerEngine
from presidio_anonymizer.entities import OperatorConfig

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("presidio-proxy")

SEARXNG_URL = os.environ.get("SEARXNG_URL", "http://searxng:8080").rstrip("/")
LANG = os.environ.get("PROXY_LANG", "de")

app = FastAPI(title="PII-Masking Search Proxy")

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
        masked = anonymizer.anonymize(text=text, analyzer_results=results, operators=OPERATORS)
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
    params["q"] = mask(raw_q)
    # JSON erzwingen, damit Agent/OWUI strukturiert parsen koennen:
    params.setdefault("format", "json")

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.get(f"{SEARXNG_URL}/search", params=params)
        ctype = r.headers.get("content-type", "")
        if "application/json" in ctype:
            return JSONResponse(content=r.json(), status_code=r.status_code)
        return PlainTextResponse(content=r.text, status_code=r.status_code,
                                 media_type=ctype or "text/plain")
    except Exception as e:
        log.exception("SearXNG-Weiterleitung fehlgeschlagen")
        return JSONResponse(content={"error": str(e), "results": []}, status_code=502)
