import base64, json, os, sys, tempfile
WORK = tempfile.mkdtemp(prefix="virt_")
os.environ["SANDBOX_WORK"] = WORK
os.environ["SANDBOX_FILES_TOKEN"] = "geheim123"
sys.path.insert(0, "/home/user/cclon-loc-nv-03/code-sandbox")
from fastapi.testclient import TestClient
import run_api

c = TestClient(run_api.app)
A, B = "chat-A", "chat-B"
HA = {"Authorization": "Bearer geheim123", "X-Session-Id": A}
HB = {"Authorization": "Bearer geheim123", "X-Session-Id": B}
ok = True
def check(l, cond, extra=""):
    global ok
    print(("PASS " if cond else "FAIL ") + l + ("  " + str(extra) if not cond else ""))
    if not cond: ok = False

# Zwei Chats mit je eigener Datei + document.txt (Volltext-Eingabe)
c.post("/run", json={"code": "open('a.csv','w').write('A\\n')", "chat_id": A,
                     "files": {"document.txt": base64.b64encode(b"volltext A").decode()}})
c.post("/run", json={"code": "open('b.csv','w').write('B\\n')", "chat_id": B})

# --- Alles nach aussen ist VIRTUELL --------------------------------------
j = c.get("/files/cwd", headers=HA).json()
check("cwd virtuell '/'", j == {"cwd": "/", "home": "/", "root": {"path": "/", "label": "Chat-Dateien"}}, j)

j = c.get("/files/list", headers=HA, params={"directory": "/"}).json()
check("list path virtuell", j["path"] == "/", j["path"])
check("Chat A sieht nur a.csv", [e["name"] for e in j["entries"]] == ["a.csv"], j["entries"])
check("document.txt ausgeblendet", "document.txt" not in [e["name"] for e in j["entries"]], j["entries"])
check("kein Container-Pfad in der Antwort", WORK not in json.dumps(j), j)

j = c.get("/files/list", headers=HB, params={"directory": "/"}).json()
check("Chat B sieht nur b.csv", [e["name"] for e in j["entries"]] == ["b.csv"], j["entries"])

# --- Der Fall aus dem Screenshot: Client fragt das Volume-Wurzelverzeichnis
j = c.get("/files/list", headers=HA, params={"directory": "/home/sandbox/work"}).json()
check("Volume-Wurzel -> eigener Chat", [e["name"] for e in j["entries"]] == ["a.csv"], j)
check("Volume-Wurzel -> path bleibt '/'", j["path"] == "/", j["path"])
j = c.get("/files/list", headers=HA, params={"directory": WORK}).json()
check("echter Volume-Pfad -> eigener Chat", [e["name"] for e in j["entries"]] == ["a.csv"], j)

# Alt-Client mit absolutem Chat-Pfad (Rueckwaertskompatibilitaet)
j = c.get("/files/list", headers=HA, params={"directory": f"{WORK}/chat_{A}/"}).json()
check("alter absoluter Chat-Pfad funktioniert", [e["name"] for e in j["entries"]] == ["a.csv"], j)

# --- Fremder Chat ist NICHT mehr adressierbar ----------------------------
for p in (f"/chat_{B}/b.csv", f"{WORK}/chat_{B}/b.csv", "../chat_{}/b.csv".format(B)):
    r = c.get("/files/read", headers=HA, params={"path": p})
    check(f"kein Zugriff auf Chat B via {p[:28]}", r.status_code in (403, 404), r.status_code)

# --- Lesen/Download/ZIP mit virtuellen Pfaden ----------------------------
r = c.get("/files/read", headers=HA, params={"path": "/a.csv"})
check("read virtuell", r.status_code == 200 and r.json()["path"] == "/a.csv", r.text)
r = c.get("/files/view", headers=HA, params={"path": "/a.csv"})
check("view virtuell", r.status_code == 200 and r.content == b"A\n", r.status_code)
r = c.post("/files/archive", headers=HA, json={"paths": ["/a.csv"]})
check("archive virtuell", r.status_code == 200 and r.content[:2] == b"PK", r.status_code)

# --- Schreiben liefert virtuelle Pfade -----------------------------------
r = c.post("/files/mkdir", headers=HA, json={"path": "/unter"})
check("mkdir virtuell", r.json()["path"] == "/unter", r.text)
r = c.post("/files/move", headers=HA, json={"source": "/a.csv", "destination": "/unter"})
check("move virtuell", r.json() == {"source": "/a.csv", "destination": "/unter/a.csv"}, r.text)
r = c.post("/files/upload", headers=HA, params={"directory": "/"},
           files={"file": ("hoch.txt", b"xyz")})
check("upload virtuell + real_path", r.json()["path"] == "/hoch.txt"
      and r.json()["real_path"].endswith(f"chat_{A}/hoch.txt"), r.text)
r = c.request("DELETE", "/files/delete", headers=HA, params={"path": "/hoch.txt"})
check("delete virtuell", r.json() == {"path": "/hoch.txt", "type": "file"}, r.text)
r = c.request("DELETE", "/files/delete", headers=HA, params={"path": "/"})
check("Chat-Ordner nicht loeschbar", r.status_code == 400, r.status_code)

# --- cwd merken/wiederholen ---------------------------------------------
c.post("/files/cwd", headers=HA, json={"path": "/unter"})
check("cwd gemerkt", c.get("/files/cwd", headers=HA).json()["cwd"] == "/unter",
      c.get("/files/cwd", headers=HA).json())
c.post("/files/cwd", headers=HA, json={"path": f"{WORK}/chat_{B}/"})
check("cwd auf fremden Chat -> geclampt", c.get("/files/cwd", headers=HA).json()["cwd"] == "/",
      c.get("/files/cwd", headers=HA).json())

# --- Ausbruch ------------------------------------------------------------
for bad in ["../../etc/passwd", "/etc/passwd", "/unter/../../.."]:
    r = c.get("/files/read", headers=HA, params={"path": bad})
    check(f"Ausbruch blockiert: {bad}", r.status_code in (403, 404), r.status_code)

print("\n" + ("VIRTUELLE PFADE OK" if ok else "FEHLER"))
sys.exit(0 if ok else 1)
