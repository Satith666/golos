"""Сценарий из журнала пользователя: запуск, затем смена микрофона и «Слышать себя» из окна программы."""
import sys, os, time, json, tempfile, threading, urllib.request
src = sys.argv[1] if len(sys.argv) > 1 else None
root = tempfile.mkdtemp()
os.environ.update(GOLOS_DATA=root, GOLOS_NO_BROWSER="1", GOLOS_NO_TRAY="1", GOLOS_URL_FILE=root + "/url")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths
paths.use(src=paths.Path(src) if src else paths.ROOT)
import app as A
t = threading.Thread(target=A.main, daemon=True); t.start()
for _ in range(80):
    if os.path.exists(root + "/url") and open(root + "/url").read(): break
    time.sleep(.1)
url = open(root + "/url").read()
op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
def do(**cmd):
    r = urllib.request.Request(url + "api/do", json.dumps(cmd).encode(), {"X-Golos": "1", "Content-Type": "application/json"})
    return json.loads(op.open(r).read())
def meters():
    return json.loads(op.open(url + "api/meters").read())
time.sleep(1)
print("после запуска:", meters()["live"], meters().get("errors", meters().get("error")))
do(action="set_device", role="mic", name="Микрофон (Realtek(R) Audio)"); time.sleep(.8)
m = meters(); print("после смены микрофона из окна:", m["live"], m.get("errors", m.get("error")))
do(action="set_monitor", value=True); time.sleep(.8)
m = meters(); print("после «Слышать себя»:", m["live"], m.get("errors", m.get("error")))
time.sleep(4)
m = meters(); print("через 4 с (автоповтор):", m["live"], m.get("errors", m.get("error")), "уровень", round(m["in"], 2))
import sounddevice as sd
bad = sd.STATE["wrong_thread"]
print("вызовов звука не из того потока:", len(bad), sorted(set(bad))[:6])
assert not bad and m["live"] and not m["errors"], "звук должен работать после смены устройств"
print("ОК")
os._exit(0)
