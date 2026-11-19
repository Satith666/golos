import sys, os, time, tempfile, logging, io
root = tempfile.mkdtemp(); os.environ.update(GOLOS_DATA=root)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths; paths.use()
import audio_io
from audio_io import AudioEngine
stream = io.StringIO(); h = logging.StreamHandler(stream); logging.getLogger("golos").addHandler(h); logging.getLogger("golos").setLevel(logging.INFO)
e = AudioEngine()
import sounddevice as sd
MIC = MIC_MME = "Микрофон (Realtek(R) Audio)"
CABLE, SPK = "CABLE Input (VB-Audio Virtual Cable)", "Динамики (Realtek(R) Audio)"
IN_USE = "Error opening InputStream: Unanticipated host error [PaErrorCode -9999]: 'x' [Windows WASAPI error -2004287478]"

print("1) WASAPI не запускает микрофон -> запасной драйвер MME")
sd.STATE["fail_start"][(MIC, "Windows WASAPI")] = "Error starting stream: Unanticipated host error [PaErrorCode -9999]: 'x' [Windows WASAPI error -2147024891]"
e.start(MIC, CABLE, None, False); time.sleep(.4)
print("   работает:", e.input is not None, "драйвер:", e.input.host, "ошибки:", e.error_list())
assert e.input.host == "MME" and not e.errors
sd.STATE["fail_start"].clear()

print("2) «Слышать себя» сломан, остальное работает")
sd.STATE["fail_open"][(SPK, "Windows WASAPI")] = "Error opening OutputStream: Unanticipated host error [PaErrorCode -9999]: 'x' [Windows WASAPI error -2004287484]"
e.start(MIC, CABLE, SPK, False); time.sleep(.5)
print("   микрофон:", e.input is not None, "кабель:", e.cable is not None, "монитор:", e.monitor is not None)
print("   ошибка:", e.error_list())
assert e.input and e.cable and not e.monitor and "вывод для «Слышать себя»" in e.error_list()[0] and "отключилось" in e.error_list()[0]
cap0 = len(sd.CAPTURE.get(CABLE, []))
time.sleep(.5); assert len(sd.CAPTURE[CABLE]) > cap0, "кабель должен получать звук"
for _ in range(3): e.watchdog()
print("   повтор назначен через", round(e.out_next["monitor"] - time.monotonic(), 1), "с")
sd.STATE["fail_open"].clear(); e.out_next["monitor"] = 0; e.watchdog(); time.sleep(.3)
print("   после починки: монитор", e.monitor is not None, "ошибки", e.error_list())
assert e.monitor is not None and not e.errors

print("3) микрофон занят другой программой на всех драйверах")
for host in ("Windows WASAPI", "MME"):
    sd.STATE["fail_open"][(MIC if host != "MME" else MIC_MME, host)] = IN_USE
log0 = len(stream.getvalue().splitlines())
e.start(MIC, CABLE, SPK, False)
print("   ошибка:", e.error_list()); assert "заняла другая программа" in e.error_list()[0] and e.cable is None
delays = []
for i in range(4):
    e.next_retry = 0; e.watchdog(); delays.append(round(e.next_retry - time.monotonic()))
print("   паузы между повторами:", delays, "с")
assert delays == [6, 12, 24, 30]
log_lines = stream.getvalue().splitlines()[log0:]
print("   строк в журнале за 5 попыток:", len(log_lines))
sd.STATE["fail_open"].clear()
e.next_retry = 0; e.watchdog(); time.sleep(.3)
print("   освободился:", e.input is not None, e.cable is not None, e.monitor is not None, e.error_list())
assert e.input and e.cable and e.monitor and not e.errors and e.mic_fails == 0

print("4) устройство пропало")
e.start("Микрофон (Blue Yeti)", CABLE, None, False); print("   ", e.error_list()); assert "Не найдено" in e.error_list()[0]

print("5) отчёт")
e.start(MIC, CABLE, None, False)
sd.STATE["fail_open"][(SPK, "Windows WASAPI")] = IN_USE
e.start(MIC, CABLE, SPK, False)
rep = e.report(); print("   " + "\n   ".join(rep.splitlines()[:6])); print("   ...строк:", len(rep.splitlines()))
assert "Неудачные попытки (monitor)" in rep and "WASAPI error -2004287478" in rep
sd.STATE["fail_open"].clear()
e.stop()
print("вызовов не из потока звука:", len(sd.STATE["wrong_thread"]))
assert not sd.STATE["wrong_thread"]
print("ОК"); os._exit(0)
