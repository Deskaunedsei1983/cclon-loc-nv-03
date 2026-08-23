import base64
import os
import sys
import tempfile

WORK = tempfile.mkdtemp(prefix="sbwork_")
os.environ["SANDBOX_WORK"] = WORK
os.environ["SANDBOX_FILES_TOKEN"] = "geheim123"
sys.path.insert(0, "/home/user/cclon-loc-nv-03/code-sandbox")

from fastapi.testclient import TestClient  # noqa: E402
import run_api  # noqa: E402

c = TestClient(run_api.app)
H = {"Authorization": "Bearer geheim123", "X-Session-Id": "chat-abc-123"}
BAD = {"Authorization": "Bearer falsch", "X-Session-Id": "chat-abc-123"}

ok = True


def check(label, cond, extra=""):
    global ok
    print(("PASS " if cond else "FAIL ") + label + ("  " + str(extra) if not cond else ""))
    if not cond:
        ok = False


# 1) Auth
check("401 ohne Token", c.get("/files/list").status_code == 401)
check("401 bei falschem Token", c.get("/files/list", headers=BAD).status_code == 401)

# 2) /api/config -> terminal aus
r = c.get("/api/config", headers=H)
check("config features.terminal=false", r.status_code == 200 and r.json()["features"]["terminal"] is False, r.text)

# 3) Code ausfuehren -> Datei im Chat-Ordner
code = "open('bericht.csv','w').write('a,b\\n1,2\\n')\nprint('fertig')"
r = c.post("/run", json={"code": code, "chat_id": "chat-abc-123"})
j = r.json()
check("run rc=0", j.get("returncode") == 0, j)
check("run stdout", "fertig" in j.get("stdout", ""), j.get("stdout"))
names = [f["name"] for f in j.get("files", [])]
check("run meldet bericht.csv", names == ["bericht.csv"], names)
check("run work_dir = chat-Ordner", j["work_dir"].endswith("chat_chat-abc-123"), j["work_dir"])
check("kein snippet im Ergebnis", not any(n.startswith(".snippet") for n in names), names)

# 3b) Zweiter Lauf sieht die Datei des ersten (iteratives Arbeiten)
r = c.post("/run", json={"code": "print(open('bericht.csv').read().strip())", "chat_id": "chat-abc-123"})
j2 = r.json()
check("Folgelauf sieht alte Datei", "a,b" in j2.get("stdout", ""), j2)
check("Folgelauf meldet keine neuen Dateien", j2.get("files") == [], j2.get("files"))

# 4) cwd
r = c.get("/files/cwd", headers=H)
j = r.json()
check("cwd root label", j["root"]["label"] == "Chat-Dateien", j)
check("cwd virtuell '/'", j["cwd"] == "/" and j["root"]["path"] == "/", j)

# 5) list
r = c.get("/files/list", headers=H, params={"directory": "/"})
entries = r.json()["entries"]
check("list zeigt bericht.csv", [e["name"] for e in entries] == ["bericht.csv"], entries)
check("list liefert mtime in Sekunden", entries[0]["modified"] > 1_600_000_000 and entries[0]["modified"] < 4_000_000_000, entries)

# 6) read
r = c.get("/files/read", headers=H, params={"path": "bericht.csv"})
check("read liefert Inhalt", r.json().get("content", "").startswith("a,b"), r.text)

# 7) view (Download)
r = c.get("/files/view", headers=H, params={"path": "bericht.csv"})
check("view Content-Disposition", 'filename="bericht.csv"' in r.headers.get("content-disposition", ""), r.headers)
check("view Bytes", r.content.startswith(b"a,b"), r.content[:20])

# 8) mkdir + move + list
check("mkdir", c.post("/files/mkdir", headers=H, json={"path": "unterordner"}).status_code == 200)
r = c.post("/files/move", headers=H, json={"source": "bericht.csv", "destination": "unterordner"})
check("move in Unterordner", r.status_code == 200 and r.json()["destination"] == "/unterordner/bericht.csv", r.text)
r = c.get("/files/list", headers=H, params={"directory": "unterordner"})
check("list im Unterordner", [e["name"] for e in r.json()["entries"]] == ["bericht.csv"], r.text)

# 9) archive (ZIP)
r = c.post("/files/archive", headers=H, json={"paths": ["unterordner"]})
check("archive liefert ZIP", r.status_code == 200 and r.content[:2] == b"PK", r.status_code)
check("archive Dateiname", "unterordner.zip" in r.headers.get("content-disposition", ""), r.headers)

# 10) upload
r = c.post("/files/upload", headers=H, params={"directory": "/"},
           files={"file": ("hoch.txt", b"hallo welt", "text/plain")})
check("upload", r.status_code == 200 and r.json()["size"] == 10, r.text)

# 11) Pfad-Ausbruch: LESEN ausserhalb des Chat-Ordners nie erfolgreich
for bad in ["../../etc/passwd", "/etc/passwd", "unterordner/../../..", "../geheim.txt"]:
    r = c.get("/files/read", headers=H, params={"path": bad})
    check(f"Ausbruch blockiert: {bad}", r.status_code in (403, 404), r.status_code)
# Auflisten endet nie in einer Sackgasse -> clamp auf den Chat-Ordner
for stale in ["../", "/home/sandbox/work", "/etc"]:
    r = c.get("/files/list", headers=H, params={"directory": stale})
    check(f"'{stale}' wird geclampt statt Fehler", r.status_code == 200 and r.json()["path"] == "/",
          r.status_code)

# 12) Chat-Trennung
H2 = {"Authorization": "Bearer geheim123", "X-Session-Id": "anderer-chat"}
r = c.get("/files/list", headers=H2, params={"directory": "/"})
check("anderer Chat sieht nichts", r.json()["entries"] == [], r.text)

# 13) Chat-Ordner selbst nicht loeschbar
r = c.request("DELETE", "/files/delete", headers=H, params={"path": "/"})
check("Chat-Ordner nicht loeschbar", r.status_code == 400, r.status_code)
r = c.request("DELETE", "/files/delete", headers=H, params={"path": "hoch.txt"})
check("Datei loeschbar", r.status_code == 200 and r.json()["type"] == "file", r.text)

# 14) Binaerdatei -> read gibt Platzhalter-Content-Type
c.post("/run", json={"code": "open('bin.dat','wb').write(bytes(range(256)))", "chat_id": "chat-abc-123"})
r = c.get("/files/read", headers=H, params={"path": "bin.dat"})
check("read binaer -> octet-stream", r.headers.get("content-type", "").startswith("application/octet-stream"), r.headers)

# 15) Fehler als JSON-Objekt (FileNav liest res.json())
r = c.get("/files/read", headers=H, params={"path": "gibtsnicht.txt"})
check("404 als JSON", r.status_code == 404 and "error" in r.json(), r.text)

# 16) OpenAPI enthaelt keine Datei-Werkzeuge
paths = c.get("/openapi.json").json().get("paths", {})
check("openapi ohne /files/*", not any(p.startswith("/files") for p in paths), list(paths))

# 17) Ports
check("ports leer", c.get("/ports", headers=H).json() == {"ports": []})

# 18) Eingabedateien landen nicht im Ergebnis
r = c.post("/run", json={"code": "print(open('doc.txt').read())",
                         "files": {"doc.txt": base64.b64encode(b"volltext").decode()},
                         "chat_id": "chat-abc-123"})
j = r.json()
check("Eingabedatei nicht als Ergebnis", "doc.txt" not in [f["name"] for f in j["files"]], j["files"])
check("Eingabedatei lesbar", "volltext" in j["stdout"], j["stdout"])


# ── Neu: Scope-Regeln (stale Pfade duerfen keine Sackgasse sein) ────────────
# Zweiten Chat mit Inhalt anlegen
c.post("/run", json={"code": "open('fremd.csv','w').write('q\\n')", "chat_id": "anderer-chat"})
FREMD = f"{WORK}/chat_anderer-chat/fremd.csv"
FREMD_DIR = f"{WORK}/chat_anderer-chat/"

# list mit STALE Pfad (anderer Chat) -> clamp auf eigenen Ordner, kein 403
r = c.get("/files/list", headers=H, params={"directory": FREMD_DIR})
check("list clamped statt 403", r.status_code == 200, r.text)
check("list clamped liefert EIGENE Dateien", "fremd.csv" not in [e["name"] for e in r.json()["entries"]], r.text)

# genau der Fall aus dem Log: _ohne_chat mit gesetzter Session
r = c.get("/files/list", headers=H, params={"directory": f"{WORK}/_ohne_chat/"})
check("_ohne_chat mit Session -> clamp (kein 403)", r.status_code == 200, r.status_code)
r = c.post("/files/cwd", headers=H, json={"path": f"{WORK}/_ohne_chat/"})
check("POST cwd _ohne_chat -> clamp", r.status_code == 200 and r.json()["cwd"] == "/", r.text)

# Fremde Chats sind gar nicht mehr adressierbar (virtuelle Pfade)
check("read fremde Datei geblockt", c.get("/files/read", headers=H, params={"path": FREMD}).status_code in (403, 404))
check("view fremde Datei geblockt", c.get("/files/view", headers=H, params={"path": FREMD}).status_code in (403, 404))
check("archive fremde Datei geblockt", c.post("/files/archive", headers=H, json={"paths": [FREMD]}).status_code in (403, 404))

# Schreiben quer bleibt verboten
check("delete fremd geblockt", c.request("DELETE", "/files/delete", headers=H, params={"path": FREMD}).status_code in (403, 404))
check("mkdir fremd landet im EIGENEN Ordner",
      c.post("/files/mkdir", headers=H, json={"path": f"{WORK}/chat_anderer-chat/neu"}).json()["path"] == "/chat_anderer-chat/neu")
check("move fremd geblockt", c.post("/files/move", headers=H, json={"source": FREMD, "destination": "x.csv"}).status_code in (403, 404))
# Der Pfad des fremden Chats wird zum blossen NAMEN im eigenen Ordner (den die
# mkdir-Zeile darueber angelegt hat) -> landet im EIGENEN Chat, nicht im fremden.
_r = c.post("/files/upload", headers=H, params={"directory": FREMD_DIR}, files={"file": ("y.txt", b"z")})
check("upload landet im EIGENEN Ordner", _r.json().get("path") == "/chat_anderer-chat/y.txt", _r.text)
check("fremder Chat bleibt unberuehrt",
      [e["name"] for e in c.get("/files/list", headers=H2, params={"directory": "/"}).json()["entries"]] == ["fremd.csv"],
      c.get("/files/list", headers=H2, params={"directory": "/"}).json())

# Absolute Systempfade sind nur noch Namen IM Chat-Ordner -> existieren nicht
for bad in ["/etc/passwd", "/home/sandbox"]:
    r = c.get("/files/read", headers=H, params={"path": bad})
    check(f"Systempfad nicht lesbar: {bad}", r.status_code in (403, 404), r.status_code)

print("\n" + ("ALLE TESTS BESTANDEN" if ok else "FEHLER"))
sys.exit(0 if ok else 1)
