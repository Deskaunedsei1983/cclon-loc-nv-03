"""Prueft die Artefakt-Logik aus agent/common.py isoliert (ohne die schweren
Imports des Agenten): Auftrag aus der Anfrage + Rettungsnetz fuer Code-Bloecke."""
import asyncio, base64, json, os, re, sys, logging

SRC = open("/home/user/cclon-loc-nv-03/agent/common.py").read()

def block(start, end):
    a = SRC.index(start); b = SRC.index(end)
    return SRC[a:b]

ns = {"os": os, "re": re, "base64": base64, "json": json,
      "log": logging.getLogger("test"), "httpx": type("x", (), {"AsyncClient": object})}
exec(block("_CREATE_VERB_RE = re.compile(", "def system_prompt_now"), ns)
exec(block("_FENCE_RE = re.compile(", "async def t_run_code"), ns)

# Stubs fuer die Sandbox-Anbindung
ns["_RUN_FILES"] = {}
ns["SANDBOX_WORK_PREFIX"] = "/home/sandbox/work"
ns["SANDBOX_WORK_MOUNT"] = "/sandbox-work"
ns["_guess_ctype"] = lambda n: "application/x-ipynb+json" if n.endswith(".ipynb") else "text/plain"
gespeichert = {}
ns["_files_api_headers"] = lambda: {"X-Session-Id": "chat-1"}
async def _put(http, name, data):
    gespeichert[name] = data
    return f"/home/sandbox/work/chat_1/{name}"
ns["_put_into_chat_folder"] = _put

ok = True
def check(l, cond, extra=""):
    global ok
    print(("PASS " if cond else "FAIL ") + l + ("  " + str(extra) if not cond else ""))
    if not cond: ok = False

hint = ns["artifact_request_hint"]

# --- Auftrag aus der Anfrage --------------------------------------------
check("Notebook erkannt", ".ipynb" in hint("erstelle ein komplexes produktreifes jupyter notebook"))
check("run_code gefordert", "'run_code'" in hint("schreibe ein jupyter notebook"))
check("Excel erkannt", ".xlsx" in hint("erstelle mir bitte eine Excel-Auswertung"))
check("Word erkannt", ".docx" in hint("schreibe ein Word-Dokument dazu"))
check("PowerPoint erkannt", ".pptx" in hint("baue eine Praesentation mit 10 Folien"))
check("CSV erkannt", ".csv" in hint("generiere eine csv mit den Kennzahlen"))
check("Skript erkannt", ".py" in hint("schreib mir ein python skript"))
check("ohne Verb kein Auftrag", hint("was ist ein jupyter notebook?") == "",
      hint("was ist ein jupyter notebook?")[:50])
check("ohne Artefakt kein Auftrag", hint("erstelle eine Zusammenfassung") == "")
check("leere Anfrage", hint("") == "")

# --- Rettungsnetz --------------------------------------------------------
salvage = ns["salvage_code_blocks"]
run = asyncio.run

nb = json.dumps({"cells": [{"cell_type": "code", "source": ["x = 1\n"] * 200,
                            "metadata": {}, "outputs": [], "execution_count": None}],
                 "metadata": {}, "nbformat": 4, "nbformat_minor": 5})
antwort = f"Hier das Notebook:\n\n```json\n{nb}\n```\n\nViel Erfolg."
ns["_RUN_FILES"].clear(); gespeichert.clear()
neu = run(salvage(None, antwort))
check("Notebook-JSON erkannt", "notebook.ipynb" in gespeichert, list(gespeichert))
check("Block aus der Antwort entfernt", "```" not in neu, neu[:200])
check("Hinweis statt Inhalt", "als Datei gespeichert" in neu, neu[:200])
check("Text drumherum bleibt", neu.startswith("Hier das Notebook:") and neu.endswith("Viel Erfolg."), neu[:80])
check("als Download registriert", "notebook.ipynb" in ns["_RUN_FILES"], ns["_RUN_FILES"])
check("Pfad auf den OWUI-Mount gemappt",
      ns["_RUN_FILES"]["notebook.ipynb"]["path"] == "/sandbox-work/chat_1/notebook.ipynb",
      ns["_RUN_FILES"].get("notebook.ipynb"))

# Kleiner Block bleibt unangetastet
ns["_RUN_FILES"].clear(); gespeichert.clear()
klein = "Beispiel:\n\n```python\nprint('hallo')\n```\n"
check("kleiner Block bleibt inline", run(salvage(None, klein)) == klein and not gespeichert)

# Wurde schon eine Datei erzeugt -> Netz greift NICHT
ns["_RUN_FILES"].clear(); gespeichert.clear()
ns["_RUN_FILES"]["schon.csv"] = {"size": 1}
check("kein Eingriff bei vorhandener Datei", run(salvage(None, antwort)) == antwort and not gespeichert)

# Python-Block -> .py
ns["_RUN_FILES"].clear(); gespeichert.clear()
py = "```python\n" + "print(1)\n" * 400 + "```"
run(salvage(None, py))
check("grosser Python-Block -> skript.py", "skript.py" in gespeichert, list(gespeichert))

# Mehrere grosse Bloecke -> durchnummeriert
ns["_RUN_FILES"].clear(); gespeichert.clear()
zwei = "```python\n" + "a=1\n" * 400 + "```\n\ntext\n\n```python\n" + "b=2\n" * 400 + "```"
run(salvage(None, zwei))
check("zwei Bloecke -> zwei Dateien", sorted(gespeichert) == ["skript.py", "skript_2.py"], list(gespeichert))

# Ohne Datei-API (kein Token/Chat) passiert nichts
ns["_RUN_FILES"].clear(); gespeichert.clear()
ns["_files_api_headers"] = lambda: None
check("ohne Datei-API unveraendert", run(salvage(None, antwort)) == antwort and not gespeichert)

print("\n" + ("ARTEFAKT-LOGIK OK" if ok else "FEHLER"))
sys.exit(0 if ok else 1)
