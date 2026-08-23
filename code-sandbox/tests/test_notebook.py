"""Notebook-Ausfuehrung: die drei Endpunkte, die OWUIs NotebookView braucht."""
import json, os, sys, tempfile
WORK = tempfile.mkdtemp(prefix="nb_")
os.environ["SANDBOX_WORK"] = WORK
os.environ["SANDBOX_FILES_TOKEN"] = "geheim123"
os.environ["SANDBOX_NB_CELL_TIMEOUT"] = "20"
sys.path.insert(0, "/home/user/cclon-loc-nv-03/code-sandbox")
from fastapi.testclient import TestClient
import run_api

c = TestClient(run_api.app)
CHAT = "chat-nb"
H = {"Authorization": "Bearer geheim123", "X-Session-Id": CHAT}
HB = {"Authorization": "Bearer geheim123", "X-Session-Id": "anderer"}
ok = True
def check(l, cond, extra=""):
    global ok
    print(("PASS " if cond else "FAIL ") + l + ("  " + str(extra) if not cond else ""))
    if not cond: ok = False

nb = {"cells": [{"cell_type": "code", "source": ["print('hallo')\n"], "metadata": {},
                 "outputs": [], "execution_count": None}],
      "metadata": {}, "nbformat": 4, "nbformat_minor": 5}
c.post("/files/upload", headers=H, params={"directory": "/"},
       files={"file": ("demo.ipynb", json.dumps(nb).encode())})

# 1) Sitzung anlegen
r = c.post("/notebooks", headers=H, json={"path": "/demo.ipynb"})
check("Kernel startet", r.status_code == 200 and r.json().get("status") == "ready", r.text)
sid = r.json().get("id")

# 2) stdout
r = c.post(f"/notebooks/{sid}/execute", headers=H, json={"cell_index": 0, "source": "print('hallo')"})
j = r.json()
check("stdout als stream-Output", j["status"] == "ok"
      and any(o["output_type"] == "stream" and "hallo" in o["text"] for o in j["outputs"]), j)
check("execution_count gesetzt", j.get("execution_count") == 1, j.get("execution_count"))

# 3) Rueckgabewert
j = c.post(f"/notebooks/{sid}/execute", headers=H, json={"cell_index": 0, "source": "40 + 2"}).json()
check("execute_result", any(o["output_type"] == "execute_result"
                            and o["data"].get("text/plain") == "42" for o in j["outputs"]), j)

# 4) Zustand bleibt zwischen Zellen erhalten
c.post(f"/notebooks/{sid}/execute", headers=H, json={"cell_index": 0, "source": "x = 7"})
j = c.post(f"/notebooks/{sid}/execute", headers=H, json={"cell_index": 1, "source": "print(x * 6)"}).json()
check("Kernel-Zustand bleibt", any("42" in o.get("text", "") for o in j["outputs"]), j)

# 5) Fehler sauber gemeldet
j = c.post(f"/notebooks/{sid}/execute", headers=H, json={"cell_index": 0, "source": "1/0"}).json()
check("Fehler als error-Output", j["status"] == "error"
      and any(o["output_type"] == "error" and o["ename"] == "ZeroDivisionError" for o in j["outputs"]), j)

# 6) Arbeitsverzeichnis = Chat-Ordner
j = c.post(f"/notebooks/{sid}/execute", headers=H,
           json={"cell_index": 0, "source": "import os; print(sorted(os.listdir('.')))"}).json()
check("cwd ist der Chat-Ordner", any("demo.ipynb" in o.get("text", "") for o in j["outputs"]), j)

# 7) Datei aus der Zelle heraus schreiben -> taucht im Browser auf
c.post(f"/notebooks/{sid}/execute", headers=H,
       json={"cell_index": 0, "source": "open('aus_zelle.csv','w').write('a\\n')"})
namen = [e["name"] for e in c.get("/files/list", headers=H, params={"directory": "/"}).json()["entries"]]
check("Zelle schreibt in den Chat-Ordner", "aus_zelle.csv" in namen, namen)

# 8) Fremder Chat darf die Sitzung nicht benutzen
check("fremde Sitzung geblockt",
      c.post(f"/notebooks/{sid}/execute", headers=HB, json={"cell_index": 0, "source": "1"}).status_code == 404)
check("fremdes DELETE geblockt", c.request("DELETE", f"/notebooks/{sid}", headers=HB).status_code == 404)

# 9) Timeout bricht ab statt zu haengen
j = c.post(f"/notebooks/{sid}/execute", headers=H,
           json={"cell_index": 0, "source": "import time; time.sleep(90)"}).json()
check("Timeout -> Fehler statt Haenger", j["status"] == "error"
      and any(o.get("ename") == "Timeout" for o in j["outputs"]), j)

# 10) Aufraeumen
check("DELETE beendet den Kernel", c.request("DELETE", f"/notebooks/{sid}", headers=H).status_code == 200)
check("Sitzung danach unbekannt",
      c.post(f"/notebooks/{sid}/execute", headers=H, json={"cell_index": 0, "source": "1"}).status_code == 404)

# 11) Nicht vorhandene Datei
check("unbekannte Datei -> 404", c.post("/notebooks", headers=H, json={"path": "/gibtsnicht.ipynb"}).status_code == 404)

# 12) Nicht im OpenAPI-Schema
check("openapi ohne /notebooks",
      not any(p.startswith("/notebooks") for p in c.get("/openapi.json").json().get("paths", {})))

print("\n" + ("NOTEBOOK OK" if ok else "FEHLER"))
sys.exit(0 if ok else 1)
