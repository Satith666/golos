import sys, os, time, json, threading, urllib.request, tempfile, shutil, urllib.error
root = tempfile.mkdtemp()
os.environ.update(GOLOS_DATA=root, GOLOS_NO_BROWSER="1", GOLOS_PORT="0", GOLOS_URL_FILE=root + "/url")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths; paths.use()
import sounddevice as sd, numpy as np, app as A
a = A.App()
a.scan_devices(); a.restart_audio(); a.url = "x"
print("устройства:", json.dumps(a.devices, ensure_ascii=False))
print("mic", a.settings["mic"], "| out", a.settings["out"], "| monitor", a.settings["monitor"])
time.sleep(2.5)
e = a.engine
print("ошибка:", e.error_list(), "задержка", round(e.latency_ms), "мс", "срывов", e.underruns(), "ошибок в потоке", e.cb_errors)
cap = np.concatenate(sd.CAPTURE["CABLE Input (VB-Audio Virtual Cable)"])
print("на кабель пришло", len(cap), "отсчётов, пик", float(np.max(np.abs(cap))))
assert np.max(np.abs(cap)) > 0.1 and e.cb_errors == 0 and not e.errors

r = a.do({"action": "preset", "name": "Демон"}); print(r, a.params.pitch, a.params.formant, a.settings["preset"])
assert a.params.pitch == -9
a.do({"action": "set_param", "name": "reverb", "value": 0.5}); assert a.params.reverb == .5 and a.settings["preset"] is None
a.do({"action": "set_param", "name": "pitch", "value": 99}); assert a.params.pitch == 12
assert a.do({"action": "set_param", "name": "__class__", "value": 1})["ok"] is False
a.do({"action": "save_preset", "name": "Мой демон"}); assert a.settings["preset"] == "Мой демон"
assert a.do({"action": "save_preset", "name": "Робот"})["ok"] is False
a.do({"action": "step_preset", "step": 1}); print("следующий после своего:", a.settings["preset"])
a.do({"action": "preset", "name": "Мой демон"}); assert a.params.pitch == 12
a.do({"action": "delete_preset", "name": "Мой демон"}); assert a.settings["preset"] == "Мой голос" and a.params.pitch == 0
a.do({"action": "toggle_effect"}); assert a.params.bypass; a.do({"action": "toggle_effect"})
a.do({"action": "toggle_mute"}); assert a.params.mute; a.do({"action": "toggle_mute"})

print("-- наблюдение за собой и смена устройств")
a.do({"action": "set_monitor", "value": True}); time.sleep(.5)
assert e.monitor is not None and not e.errors
a.do({"action": "set_device", "role": "monitor", "name": "Наушники (HyperX Cloud II Wireless)"}); time.sleep(.4)
assert "Наушники (HyperX Cloud II Wireless)" in sd.CAPTURE
a.do({"action": "set_fast", "value": True}); time.sleep(.3); print("быстрый режим, N =", e.proc.n, "задержка", round(e.latency_ms))
assert e.proc.n == 1024 or e.proc.n == 1024
a.do({"action": "set_fast", "value": False})
a.do({"action": "set_monitor", "value": False}); assert e.monitor is None

print("-- отвал устройства и восстановление")
sd.STATE["kill"] = "Микрофон (Realtek(R) Audio)"; time.sleep(.3)
assert not e.input.active
e.next_retry = 0; e.watchdog(); time.sleep(.3); assert e.input is not None and e.input.active; print("восстановлено, ошибка:", e.error_list())

print("-- откат при ошибке открытия")
sd.STATE["fail_first"] = 3; a.restart_audio(); time.sleep(.2); assert e.input is not None and not e.errors; print("открылось с запасного варианта:", e.input.rate, e.input.ch)

print("-- молчащий микрофон")
sd.STATE["silent"] = True; time.sleep(.2); e.started_at = 0; e.silence_peak = 0; time.sleep(.2); e.watchdog(); print("mic_silent:", e.mic_silent); assert e.mic_silent
sd.STATE["silent"] = False; time.sleep(.2); e.started_at = 0; e.silence_peak=0; time.sleep(.2); e.watchdog(); assert not e.mic_silent

print("-- нет кабеля")
sd.STATE["cable"] = False; a.do({"action": "refresh"}); time.sleep(.2)
print("cable", a.devices["cable"], "out", a.settings["out"], "error", e.error_list())
assert a.devices["cable"] is None and a.settings["out"] is None and e.input is not None
r = a.do({"action": "set_all_apps", "value": True}); assert a.settings["all_apps"] is False; print("без кабеля:", a.all_apps_error)
sd.STATE["cable"] = True; a.do({"action": "refresh"}); assert a.settings["out"] and "CABLE" in a.settings["out"]

print("-- сохранение настроек")
a.do({"action": "preset", "name": "Рация"}); a.save()
b = A.App(); assert b.settings["preset"] == "Рация" and b.params.radio == 1.0 and not b.params.mute
print("настройки загружены")
(A.SETTINGS_PATH).write_text("{broken", encoding="utf-8"); c = A.App(); assert c.settings["preset"] == "Мой голос"; print("битый файл настроек не мешает")

e.stop(); print("ОК"); shutil.rmtree(root, ignore_errors=True); os._exit(0)
