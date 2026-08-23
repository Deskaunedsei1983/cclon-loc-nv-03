"""run_files_block(): kein sichtbarer HTML-Marker mehr, solange der
Datei-Browser laeuft."""
import os, re, sys, json as _json

SRC = open("/home/user/cclon-loc-nv-03/agent/common.py").read()
body = SRC[SRC.index("def run_files_block()"):SRC.index("# --- Rettungsnetz")]

ok = True
def check(l, cond, extra=""):
    global ok
    print(("PASS " if cond else "FAIL ") + l + ("  " + str(extra) if not cond else ""))
    if not cond: ok = False

def build(mode, token, files):
    ns = {"os": os, "FILES_MARK_BEGIN": "<!--OWUI_FILES", "FILES_MARK_END": "OWUI_FILES-->",
          "FILES_MARKER_MODE": mode, "SANDBOX_FILES_TOKEN": token,
          "SANDBOX_FILES_URL": "http://code-sandbox:8000", "_RUN_FILES": files}
    exec(body, ns)
    return ns["run_files_block"]()

F = {"product_reifes_notebook.ipynb": {"content_type": "application/x-ipynb+json",
                                       "path": "/sandbox-work/chat_x/product_reifes_notebook.ipynb",
                                       "size": 430}}

out = build("auto", "tok", F)
check("auto + Datei-API -> KEIN Marker", "OWUI_FILES" not in out, out)
check("auto -> Dateiname bleibt sichtbar", "product_reifes_notebook.ipynb (430 B)" in out, out)
check("auto -> Hinweis auf die Seitenleiste", "Files" in out, out)
check("auto -> kein JSON/Pfad im Chat", "sandbox-work" not in out and "content_type" not in out, out)

out = build("auto", "", F)          # keine Datei-API -> Marker als Notnagel
check("auto ohne Datei-API -> Marker", "OWUI_FILES" in out and "sandbox-work" in out, out[:120])

check("never -> kein Marker", "OWUI_FILES" not in build("never", "", F))
check("always -> Marker", "OWUI_FILES" in build("always", "tok", F))
check("keine Dateien -> leer", build("auto", "tok", {}) == "")

# Groessenformatierung
big = {"gross.xlsx": {"size": 3 * 1024 * 1024}}
check("MB-Formatierung", "3.0 MB" in build("auto", "tok", big), build("auto", "tok", big))

print("\n" + ("MARKER OK" if ok else "FEHLER"))
sys.exit(0 if ok else 1)
