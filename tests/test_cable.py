"""Установка VB-CABLE в один клик на имитации Windows."""
import json
import os
import sys
import tempfile
import time
import zipfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths  # noqa: E402

paths.use()
root = tempfile.mkdtemp()
os.environ.update(GOLOS_DATA=root, GOLOS_NO_BROWSER="1", GOLOS_NO_TRAY="1")

import sounddevice as sd  # noqa: E402

import app as A  # noqa: E402
import cable  # noqa: E402
import winsys  # noqa: E402
from fake_world import World, make_pack  # noqa: E402


def wait(app, states=("done", "error", "cancelled", "need_reboot"), timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        snap = app.cable_setup.snapshot()
        if snap["state"] in states:
            return snap
        time.sleep(0.05)
    raise AssertionError(f"установка зависла: {app.cable_setup.snapshot()}")


def fresh(world_kwargs=None, frozen=True):
    for name in ("settings.json",):
        try:
            (Path(root) / name).unlink()
        except FileNotFoundError:
            pass
    world = World(sd=sd, **(world_kwargs or {}))
    world._sync_sd()
    world.patch(winsys, cable)
    sys.frozen = frozen
    sys.executable = r"C:\Users\anato\Desktop\Golos.exe"
    a = A.App()
    a.engine.refresh()
    a.scan_devices()
    a.restart_audio()
    return world, a


print("== вспомогательные функции")
urls = cable.pack_urls('x VBCABLE_Driver_Pack47.zip y VBCABLE_Driver_Pack46.zip')
print(urls[:3])
assert urls[0].endswith("Pack47.zip") and all(u.startswith(cable.DOWNLOAD_PREFIX) for u in urls)
assert cable.pack_urls(None)[0].endswith("Pack46.zip")
names = ["VBCABLE_Setup.exe", "VBCABLE_Setup_x64.exe", "VBCABLE_Setup_arm64.exe", "VBCABLE_ControlPanel.exe"]
assert cable.pick_setup(names, "AMD64") == "VBCABLE_Setup_x64.exe"
assert cable.pick_setup(names, "ARM64") == "VBCABLE_Setup_arm64.exe"
assert cable.pick_setup(names[:2], "ARM64") == "VBCABLE_Setup_x64.exe"
assert cable.pick_setup(names, "x86") == "VBCABLE_Setup.exe"
assert cable.signer_name("CN=Vincent Burel, O=Vincent Burel, C=FR") == "Vincent Burel"
bad_zip = Path(root) / "bad.zip"
bad_zip.write_bytes(make_pack({"../../evil.exe": b"x"}))
try:
    cable.safe_extract(bad_zip, Path(root) / "x")
    raise AssertionError("архив с выходом за папку должен отклоняться")
except cable.SetupError as exc:
    print("архив с ../ отклонён:", exc)


class FakeResponse:
    def __init__(self, url, body=b"PK..."):
        self.url, self.body, self.headers = url, body, {"Content-Length": str(len(body))}

    def geturl(self):
        return self.url

    def read(self, n=-1):
        body, self.body = self.body, b""
        return body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


real_download = cable.__dict__["_open"]
cable._open = lambda url, timeout=25.0: FakeResponse("https://evil.example.com/x.zip")
import importlib  # noqa: E402
fresh_cable = importlib.reload(cable)
fresh_cable._open = lambda url, timeout=25.0: FakeResponse("https://evil.example.com/x.zip")
try:
    fresh_cable.download(fresh_cable.DOWNLOAD_PREFIX + "VBCABLE_Driver_Pack46.zip", Path(root) / "d.zip", lambda *a: None)
    raise AssertionError("перенаправление на чужой сайт должно останавливать загрузку")
except fresh_cable.SetupError as exc:
    print("чужой адрес отклонён:", exc)
fresh_cable._open = lambda url, timeout=25.0: FakeResponse(url, b"ok-data")
target = Path(root) / "ok.zip"
fresh_cable.download(fresh_cable.DOWNLOAD_PREFIX + "VBCABLE_Driver_Pack46.zip", target, lambda *a: None)
assert target.read_bytes() == b"ok-data"
cable = fresh_cable
A.cable = cable

print("\n== 1. обычная установка: колонки возвращаются, голос становится основным микрофоном")
world, a = fresh()
assert a.devices["cable"] is None
assert a.do({"action": "install_cable"})["ok"]
snap = wait(a)
print(snap)
print("основные колонки:", world.def_render, "| основной микрофон:", world.def_capture)
print("Golos: mic", a.settings["mic"], "| out", a.settings["out"], "| all_apps", a.settings["all_apps"],
      "| вернуть при выходе", a.settings["restore_mic"])
assert snap["state"] == "done" and snap["signer"] == "Vincent Burel"
assert set(world.def_render.values()) == {"{hp}"}, "колонки должны вернуться к наушникам"
assert set(world.def_capture.values()) == {"{cable-out}"}
assert a.settings["restore_mic"] == {"0": "{mic}", "1": "{mic}", "2": "{mic}"}
assert a.settings["mic"] == "Микрофон (USB Camera)" and a.settings["out"].startswith("CABLE Input")
assert a.settings["all_apps"] and a.settings["cable_setup"] is None
assert world.installer_runs == ["VBCABLE_Setup_x64.exe"] and world.downloads[0].endswith("Pack46.zip")
assert world.run_once is None, "запуск после перезагрузки должен быть снят"
time.sleep(0.4)
assert a.engine.cable is not None and not a.engine.errors
assert not list(Path(root, "setup").glob("vbcable-*")), "временные файлы должны удаляться"
print("выход из Golos:")
a.hotkeys = type("H", (), {"stop": lambda s: None})()
a.quit()
print("  основной микрофон после выхода:", world.def_capture)
assert set(world.def_capture.values()) == {"{mic}"}

print("\n== 2. отказ в правах администратора")
world, a = fresh()
world.uac = "decline"
a.do({"action": "install_cable"})
snap = wait(a)
print(snap["state"], "|", snap["message"])
assert snap["state"] == "cancelled" and a.settings["cable_setup"] is None
assert set(world.def_render.values()) == {"{hp}"} and not a.settings["all_apps"]
world.uac = "accept"
a.do({"action": "install_cable"})
assert wait(a)["state"] == "done"
print("повторная попытка после отказа: готово")
a.engine.stop()

print("\n== 3. подпись не та")
world, a = fresh()
world.signature = (True, "CN=Evil Corp")
a.do({"action": "install_cable"})
snap = wait(a)
print(snap["state"], "|", snap["message"])
assert snap["state"] == "error" and "Evil Corp" in snap["message"] and not world.installer_runs
world.signature = (False, "CN=Vincent Burel")
a.do({"action": "install_cable"})
assert wait(a)["state"] == "error" and not world.installer_runs
a.engine.stop()

print("\n== 4. нужна перезагрузка")
world, a = fresh()
world.reboot_needed = True
a.do({"action": "install_cable"})
snap = wait(a)
saved = json.loads(A.SETTINGS_PATH.read_text(encoding="utf-8"))
print(snap["state"], "| запуск после перезагрузки:", world.run_once, "| сохранено:", saved["cable_setup"]["stage"])
assert snap["state"] == "need_reboot" and world.run_once == '"C:\\Users\\anato\\Desktop\\Golos.exe"'
assert saved["cable_setup"]["before"]["render"]["0"] == "{hp}"
a.do({"action": "reboot"})
assert world.rebooted
a.engine.stop()
print("-- перезагрузка: Windows сделала основными кабели, Golos запускается сам")
world.reboot()
sd.STATE["cable"] = True
b = A.App()
b.engine.refresh()
b.scan_devices()
b.recover_default_mic()
b.restart_audio()
b.resume_cable_setup()
snap = wait(b)
print(snap["state"], "| колонки:", world.def_render, "| микрофон:", world.def_capture, "| вернуть:", b.settings["restore_mic"])
assert snap["state"] == "done" and set(world.def_render.values()) == {"{hp}"}
assert set(world.def_capture.values()) == {"{cable-out}"} and b.settings["restore_mic"]["0"] == "{mic}"
assert b.settings["mic"] == "Микрофон (USB Camera)" and b.settings["cable_setup"] is None
b.engine.stop()

print("\n== 5. уже установлен: только настройка, без скачивания")
world, a = fresh({"installed": True})
world.def_render = {r: "{cable-in}" for r in ("0", "1", "2")}
a.do({"action": "install_cable"})
snap = wait(a)
print(snap["state"], "| скачиваний:", len(world.downloads), "| колонки:", world.def_render)
# прежние колонки неизвестны: берётся первое настоящее устройство вывода, не кабель
assert snap["state"] == "done" and not world.downloads and set(world.def_render.values()) == {"{spk}"}
a.do({"action": "configure_cable"})
assert wait(a)["state"] == "done"
a.engine.stop()

print("\n== 6. сайт недоступен: берётся известная ссылка")
world, a = fresh()
world.page = None


def broken_page():
    raise OSError("нет сети")


cable.fetch_page = broken_page
a.do({"action": "install_cable"})
snap = wait(a)
print(snap["state"], "| скачано:", world.downloads)
assert snap["state"] == "done" and world.downloads[0].endswith("Pack46.zip")
a.engine.stop()

print("\n== 7. отчёт и окно получают ход установки")
m = a.meters()
assert m["setup"]["state"] == "done" and "scope" in m
print("ОК")
os._exit(0)
