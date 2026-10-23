"""El agente de Chati (chati-potente, prompt nuevo) frente al fallo real del
HostManager, sobre una COPIA. Concede solo permisos dentro de la copia o de
python/pyinstaller; apunta cada paso."""
import os
import re
import sys
import time

import opencode_client as oc

BASE = "http://127.0.0.1:8901"
DIR = sys.argv[1]
TASK = sys.argv[2] if len(sys.argv) > 2 else None
# la contraseña de OpenCode de la Chati que este en marcha: la instalada o la del repo (CHATI_PW)
pw = open(os.environ.get("CHATI_PW") or os.path.join(os.environ["LOCALAPPDATA"], "ChatiIA", "data",
                                                     "opencode_password.txt")).read().strip()
oc.AUTH = ("opencode", pw)

task = TASK or (f"en la carpeta {DIR.replace(os.sep, '/')} hay un programa HostManager.exe. "
        "Lo ejecuto y no hace nada. Arreglalo tu, quiero que se vea la interfaz.")
sid = oc.start_task(BASE, task, "chati-potente", DIR)
print("tarea", sid, flush=True)
t0 = time.time()
seen = 0
SAFE = ("python", "py ", "pyinstaller", "pip show", "get-", "start-process", "stop-process", "test-path", "$p")
while time.time() - t0 < 30 * 60:
    v = oc.get_task_view(BASE, sid)
    for s in v["steps"][seen:]:
        if s["kind"] == "tool":
            print(f"[{int(time.time() - t0):4d}s] {s['tool']} {s['status']}: {(s['detail'] or s['title'])[:160]}"
                  + (f"\n        salida: {s.get('output', '')[-300:]!r}" if s.get("output") else "")
                  + (f"\n        ERROR {s['error']}" if s.get("error") else ""), flush=True)
        elif s["kind"] == "text":
            print(f"[{int(time.time() - t0):4d}s] TEXTO: {s['text'][:900]}", flush=True)
    seen = len([s for s in v["steps"] if s.get("status") not in ("running", "pending")])
    for p in v["permissions"]:
        joined = " ".join(p["patterns"]).lower()
        risky = bool(re.search(r"\b(remove-item|rm|del|rmdir|format-volume|reg|set-itemproperty)\b|drivers[/\\\\]etc", joined))
        ok = not risky and (p["permission"] != "edit" or "pruebas" in joined or "host_prueba" in joined)
        print(f"   permiso {p['permission']} {p['patterns'][:2]} -> {'SI' if ok else 'NO'}", flush=True)
        oc.reply_permission(BASE, p["id"], "once" if ok else "reject")
    if v["status"] == "idle" and not v["permissions"] and not v["questions"] and len(v["steps"]) > 1:
        print(f"FIN en {int(time.time() - t0)} s; error={v['error']}", flush=True)
        break
    if v["questions"]:
        print("PREGUNTA:", v["questions"], flush=True)
        break
    time.sleep(4)



