import json, os, sys, tempfile
WORK = tempfile.mkdtemp(prefix="stage_")
os.environ["SANDBOX_WORK"] = WORK
os.environ["SANDBOX_FILES_TOKEN"] = "geheim123"
sys.path.insert(0, "/home/user/cclon-loc-nv-03/code-sandbox")
from fastapi.testclient import TestClient
import run_api

c = TestClient(run_api.app)
CHAT = "chat-nb-1"
H = {"Authorization": "Bearer geheim123", "X-Session-Id": CHAT}
ok = True
def check(label, cond, extra=""):
    global ok
    print(("PASS " if cond else "FAIL ") + label + ("  " + str(extra) if not cond else ""))
    if not cond: ok = False

nb = {"cells": [{"cell_type": "code", "source": ["print(1)\n"], "metadata": {}, "outputs": [], "execution_count": None}],
      "metadata": {}, "nbformat": 4, "nbformat_minor": 5}
raw = json.dumps(nb).encode()

# 1) Agent legt die Originaldatei ab (wie _ensure_document_staged)
r = c.post("/files/upload", headers=H, params={"directory": "/"},
           files={"file": ("Analyse.ipynb", raw)})
check("Original abgelegt", r.status_code == 200 and r.json()["size"] == len(raw), r.text)

# 2) Skip-Erkennung: list liefert Name + exakte Groesse
entries = c.get("/files/list", headers=H, params={"directory": "/"}).json()["entries"]
match = [e for e in entries if e["name"] == "Analyse.ipynb" and e["size"] == len(raw)]
check("Skip-Erkennung (Name+Groesse)", len(match) == 1, entries)

# 3) run_code sieht die ECHTE Datei und schreibt eine neue
code = ("import json\n"
        "nb = json.load(open('Analyse.ipynb'))\n"
        "nb['cells'].append({'cell_type':'markdown','source':['# neu'],'metadata':{}})\n"
        "json.dump(nb, open('Analyse_v2.ipynb','w'))\n"
        "print('cells:', len(nb['cells']))\n")
j = c.post("/run", json={"code": code, "chat_id": CHAT}).json()
check("run rc=0", j["returncode"] == 0, j)
check("Original lesbar", "cells: 2" in j["stdout"], j["stdout"])
names = [f["name"] for f in j["files"]]
check("nur die NEUE Datei gemeldet", names == ["Analyse_v2.ipynb"], names)
check("Original nicht als 'erzeugt' gemeldet", "Analyse.ipynb" not in names, names)

# 4) Beide liegen im Chat-Ordner, Browser zeigt sie
entries = [e["name"] for e in c.get("/files/list", headers=H, params={"directory": "/"}).json()["entries"]]
check("Browser zeigt Original + neue Datei", entries == ["Analyse.ipynb", "Analyse_v2.ipynb"], entries)

# 5) Download der neuen Datei
r = c.get("/files/view", headers=H, params={"path": "Analyse_v2.ipynb"})
check("Download der neuen Datei", r.status_code == 200 and b"neu" in r.content, r.status_code)

print("\n" + ("STAGING OK" if ok else "STAGING FEHLER"))
sys.exit(0 if ok else 1)
