"""Golos: изменение голоса для Windows. Точка входа."""

from __future__ import annotations

import json
import logging
import os
import secrets
import sys
import threading
import time
import traceback
import webbrowser
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

DATA_DIR = Path(os.environ.get("GOLOS_DATA") or (Path(os.environ.get("APPDATA") or Path.home()) / "Golos"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
SETTINGS_PATH = DATA_DIR / "settings.json"
INSTANCE_PATH = DATA_DIR / "instance.json"

log = logging.getLogger("golos")
log.setLevel(logging.INFO)
_handler = RotatingFileHandler(DATA_DIR / "golos.log", maxBytes=400_000, backupCount=1, encoding="utf-8")
_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
log.addHandler(_handler)

import cable  # noqa: E402
import winsys  # noqa: E402

VERSION = "2.0"
CABLE_URL = "https://vb-audio.com/Cable/"
TITLE = "Golos"
VOICE_FIELDS = ("pitch", "formant", "robot", "whisper", "metal", "metal_hz", "drive", "radio", "echo", "reverb", "trim_db")
RANGES = {
    "pitch": (-12.0, 12.0), "formant": (-12.0, 12.0),
    "robot": (0.0, 1.0), "whisper": (0.0, 1.0), "metal": (0.0, 1.0), "drive": (0.0, 1.0),
    "radio": (0.0, 1.0), "echo": (0.0, 1.0), "reverb": (0.0, 1.0),
    "gate_db": (-80.0, -20.0), "input_db": (-12.0, 24.0), "output_db": (-20.0, 12.0),
}
HOTKEYS = {"toggle": "Ctrl+Alt+V", "mute": "Ctrl+Alt+M", "next": "Ctrl+Alt+N", "prev": "Ctrl+Alt+B"}
DEFAULTS = {
    "mic": None, "out": None, "monitor": None, "monitor_on": False, "fast": False,
    "all_apps": False, "restore_mic": None, "preset": "Мой голос", "custom": {},
    "params": {}, "hidden_hint": False, "cable_setup": None, "welcomed": False,
}


def resource(name: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / name


class App:
    def __init__(self) -> None:
        from audio_io import AudioEngine
        from dsp import Params
        from presets import BUILTIN

        self.Params = Params
        self.builtin = BUILTIN
        self.lock = threading.RLock()
        self.engine = AudioEngine()
        self.settings = self._load()
        self.version = 1
        self.dirty = False
        self.notice = ""
        self.quit_event = threading.Event()
        self.last_poll = 0.0
        self.window_closed_at: Optional[float] = None
        self.window_process = None
        self.tray = None
        self.url = ""
        self.token = secrets.token_urlsafe(18)
        self.devices: dict = {}
        self.all_apps_error = ""
        self.params = Params.from_dict(self.settings["params"])
        self.engine.set_params(self.params)
        self.cable_setup = cable.CableSetup(
            DATA_DIR / "setup", self.configure_cable, self._persist_setup, self._restart_command())
        self.hotkeys = winsys.Hotkeys({
            HOTKEYS["toggle"]: lambda: self.do({"action": "toggle_effect"}),
            HOTKEYS["mute"]: lambda: self.do({"action": "toggle_mute"}),
            HOTKEYS["next"]: lambda: self.do({"action": "step_preset", "step": 1}),
            HOTKEYS["prev"]: lambda: self.do({"action": "step_preset", "step": -1}),
        })

    # ---- настройки ----

    def _load(self) -> dict:
        data = dict(DEFAULTS)
        try:
            data.update(json.loads(SETTINGS_PATH.read_text(encoding="utf-8")))
        except FileNotFoundError:
            pass
        except Exception:
            log.exception("настройки повреждены, используются значения по умолчанию")
        return data

    def save(self) -> None:
        with self.lock:
            self.settings["params"] = {k: v for k, v in self.params.to_dict().items() if k != "mute"}
            text = json.dumps(self.settings, ensure_ascii=False, indent=1)
            self.dirty = False
        temp = SETTINGS_PATH.with_suffix(".tmp")
        try:
            temp.write_text(text, encoding="utf-8")
            os.replace(temp, SETTINGS_PATH)
        except OSError:
            log.exception("не удалось сохранить настройки")

    def _changed(self) -> None:
        self.version += 1
        self.dirty = True

    # ---- звук ----

    def scan_devices(self) -> None:
        self.devices = self.engine.devices()
        s = self.settings
        d = self.devices
        if s["mic"] not in d["mics"]:
            default = d["default_mic"]
            s["mic"] = default if default in d["mics"] else (d["mics"][0] if d["mics"] else None)
        if s["out"] not in d["outputs"]:
            s["out"] = d["cable"]
        monitors = [n for n in d["outputs"] if n != d["cable"]]
        if s["monitor"] not in monitors:
            default = d["default_out"]
            s["monitor"] = default if default in monitors else (monitors[0] if monitors else None)

    def restart_audio(self) -> None:
        s = self.settings
        out = s["out"]
        monitor = s["monitor"] if s["monitor_on"] else None
        if monitor and monitor == out:
            monitor = None
        self.engine.start(s["mic"], out, monitor, bool(s["fast"]))

    # ---- основной микрофон Windows ----

    def recover_default_mic(self) -> None:
        """После аварийного завершения возвращаем обычный микрофон или включаем режим заново."""
        s = self.settings
        if s["all_apps"]:
            self._enable_all_apps()
        elif s["restore_mic"]:
            try:
                winsys.restore_default(s["restore_mic"])
            except Exception:
                log.exception("не удалось вернуть микрофон")
            s["restore_mic"] = None
            self.dirty = True

    def _enable_all_apps(self) -> None:
        self.all_apps_error = ""
        s = self.settings
        if not self.devices.get("cable_mic"):
            s["all_apps"] = False
            self.all_apps_error = "Сначала установи VB-CABLE."
            return
        try:
            saved = winsys.make_cable_default()
            cable = winsys.find_cable_capture()
            real = {role: device for role, device in saved.items() if device != cable}
            # после аварийного выхода настоящий микрофон уже записан, его не перезаписываем
            if real and not s["restore_mic"]:
                s["restore_mic"] = real
            s["all_apps"] = True
        except Exception as exc:
            log.exception("не удалось сменить основной микрофон")
            s["all_apps"] = False
            self.all_apps_error = f"Windows не дала сменить основной микрофон ({exc}). Выбери CABLE Output вручную в настройках программы."

    def _disable_all_apps(self) -> None:
        s = self.settings
        s["all_apps"] = False
        self.all_apps_error = ""
        if s["restore_mic"]:
            try:
                winsys.restore_default(s["restore_mic"])
                s["restore_mic"] = None
            except Exception as exc:
                log.exception("не удалось вернуть микрофон")
                self.all_apps_error = f"Не удалось вернуть прежний микрофон ({exc}). Выбери его в параметрах звука Windows."

    # ---- установка VB-CABLE ----

    @staticmethod
    def _restart_command() -> Optional[str]:
        if getattr(sys, "frozen", False):
            return f'"{sys.executable}"'
        return None

    def _persist_setup(self, data: Optional[dict]) -> None:
        with self.lock:
            self.settings["cable_setup"] = data
        self.save()

    @staticmethod
    def _match(name: Optional[str], names: list[str]) -> Optional[str]:
        if not name:
            return None
        for item in names:
            if item == name:
                return item
        for item in names:
            if name.startswith(item) or item.startswith(name[:31]):
                return item
        return None

    def configure_cable(self, before: dict) -> None:
        """Настройка после установки VB-CABLE. Вызывается из потока установки."""
        try:
            if winsys.restore_render(before.get("render") or {}):
                log.info("основные колонки возвращены после установки VB-CABLE")
        except Exception:
            log.exception("не удалось вернуть основные колонки")
        self.engine.refresh()
        with self.lock:
            s = self.settings
            self.devices = self.engine.devices()
            try:
                captures = dict(winsys.capture_devices())
            except Exception:
                captures = {}
            real = {role: device for role, device in (before.get("capture") or {}).items()
                    if device in captures and not winsys.is_cable_name(captures[device])}
            if real:
                s["restore_mic"] = real
                name = self._match(captures.get(real.get("0") or next(iter(real.values()))), self.devices["mics"])
                if name:
                    s["mic"] = name
            elif not s["restore_mic"] and s["mic"]:
                # прежний основной микрофон неизвестен: при выходе вернём тот, что выбран в Golos
                for device_id, device_name in captures.items():
                    if self._match(device_name, [s["mic"]]):
                        s["restore_mic"] = {"0": device_id, "1": device_id, "2": device_id}
                        break
            if self.devices.get("cable"):
                s["out"] = self.devices["cable"]
            self.scan_devices()
            self._enable_all_apps()
            self.restart_audio()
            self._changed()
            if self.all_apps_error:
                raise cable.SetupError(self.all_apps_error)
            if not self.devices.get("cable"):
                raise cable.SetupError("VB-CABLE установлен, но Golos его пока не видит. Перезагрузи компьютер.")

    def resume_cable_setup(self) -> None:
        """При запуске: закончить установку, начатую до перезагрузки."""
        saved = self.settings.get("cable_setup")
        if not isinstance(saved, dict):
            return
        if self.devices.get("cable") and self.devices.get("cable_mic"):
            log.info("заканчиваю настройку VB-CABLE после перезагрузки")
            self.cable_setup.start(saved.get("before") or {}, only_configure=True)
        elif saved.get("stage") == "need_reboot":
            self.cable_setup.mark_reboot()
        else:
            self.settings["cable_setup"] = None
            self.dirty = True

    # ---- действия ----

    def preset_names(self) -> list[str]:
        return list(self.builtin) + list(self.settings["custom"])

    def _apply_preset(self, name: str) -> None:
        values = self.builtin.get(name, self.settings["custom"].get(name))
        if values is None:
            return
        base = self.Params()
        changes = {f: values.get(f, getattr(base, f)) for f in VOICE_FIELDS}
        self.params = replace(self.params, **changes, bypass=False)
        self.settings["preset"] = name
        self.engine.set_params(self.params)
        self._changed()

    def do(self, command: dict) -> dict:
        with self.lock:
            return self._do(command)

    def _do(self, command: dict) -> dict:
        action = command.get("action")
        s = self.settings
        if action == "set_param":
            name = str(command["name"])
            if name in RANGES:
                low, high = RANGES[name]
                value = min(high, max(low, float(command["value"])))
            elif name == "denoise":
                value = bool(command["value"])
            else:
                return {"ok": False}
            self.params = replace(self.params, **{name: value})
            if name in VOICE_FIELDS:
                self.params = replace(self.params, bypass=False)
                s["preset"] = None
            self.engine.set_params(self.params)
            self._changed()
        elif action == "preset":
            self._apply_preset(str(command["name"]))
        elif action == "step_preset":
            names = self.preset_names()
            index = names.index(s["preset"]) if s["preset"] in names else 0
            self._apply_preset(names[(index + int(command["step"])) % len(names)])
        elif action == "save_preset":
            name = str(command["name"]).strip()[:32]
            if not name:
                return {"ok": False, "error": "Введи название."}
            if name in self.builtin:
                return {"ok": False, "error": "Такое название уже есть среди готовых голосов."}
            s["custom"][name] = {f: getattr(self.params, f) for f in VOICE_FIELDS}
            s["preset"] = name
            self._changed()
        elif action == "delete_preset":
            name = str(command["name"])
            if name in s["custom"]:
                del s["custom"][name]
                if s["preset"] == name:
                    self._apply_preset("Мой голос")
                self._changed()
        elif action == "toggle_effect":
            self.params = replace(self.params, bypass=not self.params.bypass)
            self.engine.set_params(self.params)
            self._changed()
        elif action == "toggle_mute":
            self.params = replace(self.params, mute=not self.params.mute)
            self.engine.set_params(self.params)
            self._changed()
        elif action == "set_device":
            role = str(command["role"])
            value = command.get("name") or None
            if role in ("mic", "out", "monitor"):
                s[role] = value
                self.restart_audio()
                self._changed()
        elif action == "set_monitor":
            s["monitor_on"] = bool(command["value"])
            self.restart_audio()
            self._changed()
        elif action == "set_fast":
            s["fast"] = bool(command["value"])
            self.restart_audio()
            self._changed()
        elif action == "set_all_apps":
            if command["value"]:
                self._enable_all_apps()
            else:
                self._disable_all_apps()
            self._changed()
        elif action == "refresh":
            self.engine.refresh()
            self.scan_devices()
            self.restart_audio()
            self._changed()
        elif action == "open_cable":
            webbrowser.open(CABLE_URL)
        elif action == "install_cable":
            self.cable_setup.start()
        elif action == "configure_cable":
            self.cable_setup.start(only_configure=True)
        elif action == "setup_reset":
            self.cable_setup.reset()
        elif action == "reboot":
            self.save()
            winsys.reboot()
        elif action == "welcomed":
            s["welcomed"] = True
            self._changed()
        elif action == "quit":
            threading.Timer(0.25, self.quit).start()
        else:
            return {"ok": False}
        return {"ok": True}

    # ---- данные для окна ----

    def state(self) -> dict:
        with self.lock:
            s = self.settings
            return {
                "v": self.version,
                "params": self.params.to_dict(),
                "preset": s["preset"],
                "builtin": self.builtin,
                "custom": s["custom"],
                "devices": self.devices,
                "mic": s["mic"], "out": s["out"], "monitor": s["monitor"],
                "monitor_on": s["monitor_on"], "fast": s["fast"], "all_apps": s["all_apps"],
                "all_apps_error": self.all_apps_error,
                "hotkeys": HOTKEYS,
                "failed_hotkeys": self.hotkeys.failed,
                "has_tray": self.tray is not None,
                "welcomed": s["welcomed"],
                "version": VERSION,
            }

    def meters(self) -> dict:
        self.last_poll = time.monotonic()
        self.window_closed_at = None
        peak_in, peak_out = self.engine.meters()
        e = self.engine
        scope = e.scope()
        return {
            "v": self.version, "in": peak_in, "out": peak_out,
            "scope": {"in": scope[0], "out": scope[1]} if scope else None,
            "setup": self.cable_setup.snapshot(),
            "errors": e.error_list(), "mic_silent": e.mic_silent, "ms": round(e.latency_ms),
            "underruns": e.underruns(), "live": e.input is not None,
        }

    def report(self) -> str:
        import platform
        lines = [f"Golos {VERSION}, {platform.platform()}, Python {platform.python_version()}"]
        try:
            lines.append(self.engine.report())
        except Exception as exc:
            lines.append(f"Отчёт о звуке недоступен: {exc}")
        with self.lock:
            lines.append(f"Настройки: монитор {self.settings['monitor_on']}, быстрый {self.settings['fast']}, "
                         f"все программы {self.settings['all_apps']}")
        lines.append(f"Установка VB-CABLE: {self.cable_setup.snapshot()}")
        try:
            tail = (DATA_DIR / "golos.log").read_text(encoding="utf-8", errors="replace").splitlines()[-60:]
            lines.append("Конец журнала:")
            lines.extend(tail)
        except OSError:
            pass
        return "\n".join(lines)

    # ---- окно и завершение ----

    def show_window(self) -> None:
        if time.monotonic() - self.last_poll < 2.5 and winsys.focus_window(TITLE):
            return
        if os.environ.get("GOLOS_NO_BROWSER"):
            return
        self.window_process = winsys.open_window(self.url, str(DATA_DIR / "window"))

    def start_tray(self) -> None:
        if os.environ.get("GOLOS_NO_TRAY"):
            return
        try:
            import pystray
            from PIL import Image, ImageDraw

            size = 64
            image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
            draw = ImageDraw.Draw(image)
            draw.ellipse((2, 2, 62, 62), fill=(25, 37, 140, 255))
            bars = (14, 26, 40, 22, 12)
            for i, h in enumerate(bars):
                x = 14 + i * 8
                draw.rounded_rectangle((x, 32 - h // 2, x + 4, 32 + h // 2), 2, fill=(255, 212, 71, 255))

            menu = pystray.Menu(
                pystray.MenuItem("Открыть Golos", lambda: self.show_window(), default=True),
                pystray.MenuItem("Изменение голоса", lambda: self.do({"action": "toggle_effect"}),
                                 checked=lambda _item: not self.params.bypass),
                pystray.MenuItem("Микрофон выключен", lambda: self.do({"action": "toggle_mute"}),
                                 checked=lambda _item: self.params.mute),
                pystray.MenuItem("Выйти", lambda: self.quit()),
            )
            self.tray = pystray.Icon("Golos", image, TITLE, menu)
            self.tray.run_detached()
        except Exception:
            log.exception("значок в трее недоступен")
            self.tray = None

    def window_closed(self) -> None:
        self.window_closed_at = time.monotonic()

    def maintenance(self) -> None:
        while not self.quit_event.wait(1.0):
            try:
                self.engine.watchdog()
                if self.dirty:
                    self.save()
                closed = self.window_closed_at
                if closed is not None and self.tray is None and time.monotonic() - closed > 2.5:
                    self.quit()
            except Exception:
                log.exception("ошибка в фоновой проверке")

    def quit(self) -> None:
        if self.quit_event.is_set():
            return
        self.quit_event.set()
        try:
            with self.lock:
                if self.settings["all_apps"]:
                    self._disable_all_apps()
                    self.settings["all_apps"] = True  # при следующем запуске режим включится снова
                self.save()
            self.engine.stop(timeout=5.0)
            self.hotkeys.stop()
            winsys.close_window(TITLE)
            if self.tray is not None:
                self.tray.stop()
        except Exception:
            log.exception("ошибка при выходе")
        try:
            INSTANCE_PATH.unlink()
        except OSError:
            pass


def make_handler(app: App, page: bytes, port_holder: dict):
    prefix = f"/{app.token}"

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args) -> None:
            pass

        def _allowed(self) -> bool:
            return self.headers.get("Host") == f"127.0.0.1:{port_holder['port']}" and self.path.startswith(prefix)

        def _send(self, code: int, body: bytes, kind: str = "application/json; charset=utf-8") -> None:
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, data: dict) -> None:
            self._send(200, json.dumps(data, ensure_ascii=False).encode("utf-8"))

        def do_GET(self) -> None:
            if not self._allowed():
                self._send(403, b"{}")
                return
            path = self.path[len(prefix):]
            if path in ("", "/"):
                self._send(200, page, "text/html; charset=utf-8")
            elif path == "/api/state":
                self._json(app.state())
            elif path == "/api/meters":
                self._json(app.meters())
            elif path == "/api/report":
                self._json({"text": app.report()})
            elif path == "/api/show":
                app.show_window()
                self._json({"ok": True})
            else:
                self._send(404, b"{}")

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(min(length, 65536))
            if not self._allowed():
                self._send(403, b"{}")
                return
            path = self.path[len(prefix):]
            if path == "/api/closed":
                app.window_closed()
                self._json({"ok": True})
            elif path == "/api/do":
                if self.headers.get("X-Golos") != "1":
                    self._send(403, b"{}")
                    return
                try:
                    self._json(app.do(json.loads(raw.decode("utf-8"))))
                except Exception as exc:
                    log.exception("ошибка команды")
                    self._send(500, json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False).encode("utf-8"))
            else:
                self._send(404, b"{}")

    return Handler


def contact_running_copy() -> bool:
    """Если программа уже запущена, показываем её окно."""
    try:
        import urllib.request
        data = json.loads(INSTANCE_PATH.read_text(encoding="utf-8"))
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        opener.open(f"{data['url']}api/show", timeout=2).read()
        return True
    except Exception:
        log.exception("не удалось связаться с запущенной копией")
        return False


def selftest() -> int:
    """Проверка собранной программы: пишет отчёт selftest.txt рядом с exe."""
    target = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path.cwd()
    lines: list[str] = []
    code = 0
    try:
        import numpy as np
        lines.append(f"numpy {np.__version__}: ok")
        from dsp import Params, VoiceProcessor
        processor = VoiceProcessor(48000)
        processor.set_params(Params(pitch=-5, formant=-2, reverb=0.3))
        out = processor.process(np.zeros(9600, dtype=np.float32))
        lines.append(f"обработка голоса: ok ({len(out)} отсчётов)")
    except Exception:
        code = 1
        lines.append("ОШИБКА обработки:\n" + traceback.format_exc())
    try:
        from audio_io import AudioEngine
        engine = AudioEngine()
        lines.append(f"PortAudio: {engine.portaudio_version()}")
        found = engine.devices()
        lines.append("микрофоны: " + "; ".join(found["mics"]))
        lines.append("выходы: " + "; ".join(found["outputs"]))
        lines.append(f"VB-CABLE: {'найден' if found['cable'] else 'не установлен'}")
        import cable as cable_module
        lines.append("ссылки на VB-CABLE: " + ", ".join(cable_module.pack_urls(None)[:2]))
    except Exception:
        code = 1
        lines.append("ОШИБКА звука:\n" + traceback.format_exc())
    if winsys.IS_WINDOWS:
        try:
            lines.append("микрофоны Windows: " + "; ".join(name for _id, name in winsys.capture_devices()))
            lines.append(f"основной микрофон читается: {bool(winsys.default_capture_ids())}")
        except Exception:
            lines.append("Смена основного микрофона недоступна:\n" + traceback.format_exc())
    if not resource("ui.html").is_file():
        code = 1
        lines.append("ОШИБКА: нет ui.html")
    lines.insert(0, f"Golos {VERSION}")
    lines.append("ИТОГ: " + ("всё работает" if code == 0 else "есть ошибки"))
    lines.append("RESULT: " + ("OK" if code == 0 else "FAILED"))
    (target / "selftest.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return code


def main() -> int:
    if "--selftest" in sys.argv:
        return selftest()
    winsys.set_app_id()
    if winsys.already_running():
        contact_running_copy()
        return 0

    app = App()
    app.scan_devices()
    app.recover_default_mic()
    app.restart_audio()
    app.resume_cable_setup()
    app.hotkeys.start()

    port_holder = {"port": 0}
    page = resource("ui.html").read_bytes()
    server = ThreadingHTTPServer(("127.0.0.1", int(os.environ.get("GOLOS_PORT", "0"))), make_handler(app, page, port_holder))
    server.daemon_threads = True
    port_holder["port"] = server.server_address[1]
    app.url = f"http://127.0.0.1:{port_holder['port']}/{app.token}/"
    threading.Thread(target=server.serve_forever, daemon=True, name="http").start()
    INSTANCE_PATH.write_text(json.dumps({"url": app.url}), encoding="utf-8")
    if os.environ.get("GOLOS_URL_FILE"):
        Path(os.environ["GOLOS_URL_FILE"]).write_text(app.url, encoding="utf-8")

    app.start_tray()
    threading.Thread(target=app.maintenance, daemon=True, name="maintenance").start()
    app.show_window()
    app.quit_event.wait()
    server.shutdown()
    logging.shutdown()
    os._exit(0)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        text = traceback.format_exc()
        log.error(text)
        winsys.message_box(f"Golos не запустился.\n\nОтчёт: {DATA_DIR / 'golos.log'}\n\n{text[-900:]}")
        sys.exit(1)
