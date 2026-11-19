"""Работа со звуковыми устройствами: микрофон -> обработка -> виртуальный кабель и наушники.

Все вызовы звуковой библиотеки (PortAudio) идут из одного отдельного потока «audio».
В Windows WASAPI требует, чтобы поток был подготовлен для COM; если открывать звук
из случайных потоков (окно программы, фоновая проверка), запуск падает с
«Unanticipated host error». Поэтому библиотека загружается и работает только в этом потоке.
"""

from __future__ import annotations

import ctypes
import logging
import os
import queue
import re
import sys
import threading
import time
from typing import Callable, Optional

import numpy as np

from dsp import FifoResampler, Params, VoiceProcessor

log = logging.getLogger("golos")

HOST_ORDER = ("Windows WASAPI", "Windows DirectSound", "MME")
ROLE_TITLES = {"mic": "микрофон", "cable": "виртуальный кабель", "monitor": "вывод для «Слышать себя»"}
RETRY_FIRST = 3.0
RETRY_MAX = 30.0


def is_cable_input(name: str) -> bool:
    low = name.lower()
    return "cable" in low and "input" in low


def is_cable_output(name: str) -> bool:
    low = name.lower()
    return "cable" in low and "output" in low


def _key(name: str) -> str:
    return name.strip().lower()[:31]


def _unique(items) -> list:
    seen = []
    for item in items:
        if item not in seen:
            seen.append(item)
    return seen


def _retry_delay(fails: int) -> float:
    return min(RETRY_MAX, RETRY_FIRST * 2 ** max(0, fails - 1))


_HRESULT_IN_USE = 0x8889000A        # AUDCLNT_E_DEVICE_IN_USE
_HRESULT_INVALIDATED = 0x88890004   # AUDCLNT_E_DEVICE_INVALIDATED
_HRESULT_ACCESS = 0x80070005        # E_ACCESSDENIED


def _hint(details: str) -> str:
    """Подсказка по коду ошибки. Текст ошибки PortAudio бывает устаревшим, поэтому смотрим только на коды."""
    codes = {int(c) & 0xFFFFFFFF for c in re.findall(r"WASAPI error (-?\d+)", details)}
    mme = {int(c) for c in re.findall(r"MME error (\d+)", details)}
    if _HRESULT_IN_USE in codes or 4 in mme:
        return "Его заняла другая программа. Закрой её или сними галочку монопольного режима в свойствах устройства."
    if _HRESULT_ACCESS in codes:
        return "Windows или антивирус запретили к нему доступ."
    if _HRESULT_INVALIDATED in codes:
        return "Устройство отключилось. Переподключи его и нажми «Проверить снова»."
    return "Попробуй переподключить его или выбрать другое. Если не поможет, нажми «Скопировать отчёт» и пришли его."


class DeviceError(Exception):
    def __init__(self, role: str, name: str, attempts: list[str]):
        self.role = role
        self.name = name
        self.attempts = attempts
        title = ROLE_TITLES.get(role, role)
        if not attempts:
            text = f"Не найдено устройство «{name}» ({title}). Проверь, что оно подключено, и нажми «Проверить снова»."
        else:
            text = f"Не удалось запустить {title} «{name}». {_hint(' '.join(attempts))}"
        super().__init__(text)
        self.message = text


def _com_init() -> None:
    """Готовим поток к COM так же, как это делает сам PortAudio."""
    if os.name != "nt":
        return
    try:
        ole = ctypes.WinDLL("ole32")
        ole.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        ole.CoInitializeEx.restype = ctypes.c_long
        ole.CoInitializeEx(None, 2)
    except Exception:
        log.exception("не удалось подготовить поток звука")


class _Output:
    def __init__(self, stream, fifo: FifoResampler):
        self.stream = stream
        self.fifo = fifo


class AudioEngine:
    def __init__(self) -> None:
        self.params = Params()
        self.sd = None
        self.proc: Optional[VoiceProcessor] = None
        self.input = None
        self.input_rate = 48000
        self.target_ms = 30.0
        self.cable: Optional[_Output] = None
        self.monitor: Optional[_Output] = None
        self.running = False
        self.config: dict = {"mic": None, "cable": None, "monitor": None, "fast": False}
        self.errors: dict[str, str] = {}
        self.attempts: dict[str, list[str]] = {}
        self.mic_fails = 0
        self.next_retry = 0.0
        self.out_fails: dict[str, int] = {}
        self.out_next: dict[str, float] = {}
        self.last_callback = 0.0
        self.started_at = 0.0
        self.silence_peak = 0.0
        self.mic_silent = False
        self.cb_errors = 0
        self.latency_ms = 0.0

        self._tasks: queue.Queue = queue.Queue()
        self._ready = threading.Event()
        self._boot_error: Optional[BaseException] = None
        self._thread = threading.Thread(target=self._worker, name="audio", daemon=True)
        self._thread.start()
        if not self._ready.wait(60.0):
            raise RuntimeError("Звуковая система Windows не ответила за минуту.")
        if self._boot_error is not None:
            raise self._boot_error

    # ---- поток звука ----

    def _worker(self) -> None:
        try:
            _com_init()
            imported_before = "sounddevice" in sys.modules
            import sounddevice
            if imported_before:
                # библиотеку уже подготовили в другом потоке: готовим заново здесь
                sounddevice._terminate()
                sounddevice._initialize()
            self.sd = sounddevice
        except BaseException as exc:
            self._boot_error = exc
            self._ready.set()
            return
        self._ready.set()
        while True:
            item = self._tasks.get()
            if item is None:
                return
            func, args, box, done = item
            try:
                box["result"] = func(*args)
            except BaseException as exc:
                box["error"] = exc
            finally:
                done.set()

    def _call(self, func: Callable, *args, timeout: float = 60.0):
        if threading.current_thread() is self._thread:
            return func(*args)
        box: dict = {}
        done = threading.Event()
        self._tasks.put((func, args, box, done))
        if not done.wait(timeout):
            raise TimeoutError("Звуковая система не отвечает.")
        if "error" in box:
            raise box["error"]
        return box.get("result")

    # ---- то, что можно вызывать из любого потока ----

    def devices(self) -> dict:
        return self._call(self._devices)

    def refresh(self) -> None:
        self._call(self._refresh)

    def start(self, mic: Optional[str], out: Optional[str], monitor: Optional[str], fast: bool) -> None:
        self._call(self._start, mic, out, monitor, fast)

    def stop(self, timeout: float = 60.0) -> None:
        self._call(self._stop, timeout=timeout)

    def watchdog(self) -> None:
        self._call(self._watchdog)

    def portaudio_version(self) -> str:
        return self._call(lambda: self.sd.get_portaudio_version()[1])

    def report(self) -> str:
        return self._call(self._report)

    def error_list(self) -> list[str]:
        errors = self.errors
        return [errors[role] for role in ("mic", "cable", "monitor") if role in errors]

    def set_params(self, params: Params) -> None:
        self.params = params
        proc = self.proc
        if proc is not None:
            proc.set_params(params)

    def meters(self) -> tuple[float, float]:
        proc = self.proc
        return proc.take_levels() if proc is not None else (0.0, 0.0)

    def scope(self) -> Optional[tuple[list, list]]:
        proc = self.proc
        return proc.take_scope() if proc is not None else None

    def underruns(self) -> int:
        total = 0
        for output in (self.cable, self.monitor):
            if output is not None:
                total += output.fifo.underruns
        return total

    # ---- список устройств ----

    def _all(self) -> list[dict]:
        sd = self.sd
        hostapis = sd.query_hostapis()
        result = []
        for index, info in enumerate(sd.query_devices()):
            item = dict(info)
            item["index"] = index
            item["host"] = hostapis[info["hostapi"]]["name"]
            result.append(item)
        return result

    def _devices(self) -> dict:
        all_devices = self._all()
        inputs = self._pick(all_devices, "max_input_channels")
        outputs = self._pick(all_devices, "max_output_channels")
        return {
            "mics": [d["name"] for d in inputs if not is_cable_output(d["name"])],
            "outputs": [d["name"] for d in outputs],
            "cable": next((d["name"] for d in outputs if is_cable_input(d["name"])), None),
            "cable_mic": next((d["name"] for d in inputs if is_cable_output(d["name"])), None),
            "default_mic": self._default_name(all_devices, "default_input_device"),
            "default_out": self._default_name(all_devices, "default_output_device"),
        }

    @staticmethod
    def _pick(devices: list[dict], channels: str) -> list[dict]:
        for host in HOST_ORDER:
            found = [d for d in devices if d["host"] == host and d[channels] > 0]
            if found:
                return found
        return [d for d in devices if d[channels] > 0]

    def _default_name(self, devices: list[dict], field: str) -> Optional[str]:
        apis = self.sd.query_hostapis()
        for host in HOST_ORDER:
            for api in apis:
                if api["name"] == host and api[field] >= 0:
                    for d in devices:
                        if d["index"] == api[field]:
                            return d["name"]
        return None

    def _refresh(self) -> None:
        """PortAudio читает список устройств только при запуске, поэтому перезапускаем его."""
        self._stop()
        self.sd._terminate()
        self.sd._initialize()

    # ---- открытие потоков ----

    def _candidates(self, name: str, channels: str) -> list[dict]:
        wanted = _key(name)
        found = [d for d in self._all() if d[channels] > 0 and _key(d["name"]) == wanted]
        order = {host: i for i, host in enumerate(HOST_ORDER)}
        return sorted(found, key=lambda d: order.get(d["host"], 99))

    def _configs(self, device: dict, channels: str) -> list[tuple]:
        max_channels = int(device[channels])
        rates = _unique([float(device["default_samplerate"]), 48000.0, 44100.0])
        counts = _unique([min(2, max_channels), 1, max_channels])
        extras: list = [None]
        if device["host"] == "Windows WASAPI":
            try:
                extras = [self.sd.WasapiSettings(auto_convert=True), None]
            except Exception:
                extras = [None]
        configs = [(extra, "low", rate, count) for extra in extras for rate in rates for count in counts]
        configs.append((extras[0], "high", rates[0], counts[0]))
        return configs

    @staticmethod
    def _close(stream) -> None:
        for method in ("abort", "close"):
            try:
                getattr(stream, method)()
            except Exception:
                pass

    def _open(self, role: str, stream_class, name: str, channels: str, callback, prepare=None):
        """Открыть и запустить поток. Перебирает драйверы Windows и форматы, пока что-то не заработает."""
        attempts: list[str] = []
        for device in self._candidates(name, channels):
            for extra, latency, rate, count in self._configs(device, channels):
                stream = None
                stage = "open"
                try:
                    stream = stream_class(
                        device=device["index"], samplerate=rate, channels=count, dtype="float32",
                        blocksize=0, latency=latency, callback=callback, extra_settings=extra,
                    )
                    stage = "start"
                    if prepare is not None:
                        prepare(int(rate))
                    stream.start()
                except Exception as exc:
                    mode = " autoconvert" if extra is not None else ""
                    attempts.append(f"{device['host']} {int(rate)} Гц {count} кан. {latency}{mode} [{stage}]: {exc}")
                    if stream is not None:
                        self._close(stream)
                    continue
                log.info("запущено (%s) %s: %s %s Гц %s кан.", ROLE_TITLES[role], device["host"], device["name"], int(rate), count)
                self.attempts[role] = attempts
                return stream, int(rate)
        self.attempts[role] = attempts
        raise DeviceError(role, name, attempts)

    def _set_error(self, role: str, text: str) -> None:
        self.errors = {**self.errors, role: text}

    def _clear_error(self, role: str) -> None:
        if role in self.errors:
            self.errors = {k: v for k, v in self.errors.items() if k != role}

    def _failed(self, exc: DeviceError, first: bool) -> None:
        self._set_error(exc.role, exc.message)
        if first:
            log.error("%s\n  " + "\n  ".join(exc.attempts or ["устройство отсутствует в списке"]), exc.message)
        else:
            log.warning("снова не удалось: %s", exc.message)

    def _make_proc(self, rate: int) -> None:
        proc = VoiceProcessor(rate, fast=bool(self.config["fast"]))
        proc.set_params(self.params)
        self.input_rate = rate
        self.target_ms = 1000.0 * proc.hop / rate * 1.5 + 12.0
        self.last_callback = time.monotonic()
        self.started_at = self.last_callback
        self.silence_peak = 0.0
        self.mic_silent = False
        self.proc = proc

    def _open_input(self, first: bool) -> bool:
        mic = self.config["mic"]
        if not mic:
            self._set_error("mic", "Выбери микрофон во вкладке «Подключение».")
            return False
        try:
            self.input, _rate = self._open("mic", self.sd.InputStream, mic, "max_input_channels", self._on_input, self._make_proc)
        except DeviceError as exc:
            self.proc = None
            self._failed(exc, first)
            return False
        self._clear_error("mic")
        return True

    def _open_output(self, role: str, first: bool) -> bool:
        name = self.config[role]
        holder: dict = {}
        try:
            stream, rate = self._open(role, self.sd.OutputStream, name, "max_output_channels", self._make_output_callback(holder))
        except DeviceError as exc:
            self._failed(exc, first)
            return False
        holder["fifo"] = FifoResampler(self.input_rate, rate, self.target_ms)
        setattr(self, role, _Output(stream, holder["fifo"]))
        self._clear_error(role)
        return True

    def _update_latency(self) -> None:
        proc = self.proc
        if proc is None or self.input is None:
            self.latency_ms = 0.0
            return
        delay = proc.latency_ms + self.target_ms
        try:
            delay += float(self.input.latency) * 1000.0
            main = self.cable or self.monitor
            if main is not None:
                delay += float(main.stream.latency) * 1000.0
        except Exception:
            pass
        self.latency_ms = delay

    def _start(self, mic: Optional[str], out: Optional[str], monitor: Optional[str], fast: bool, first: bool = True) -> None:
        self._stop_streams()
        self.config = {"mic": mic, "cable": out, "monitor": monitor, "fast": fast}
        self.running = True
        self.errors = {}
        self.out_fails = {}
        self.out_next = {}
        now = time.monotonic()
        if not self._open_input(first):
            self.mic_fails = 1
            self.next_retry = now + _retry_delay(1)
            return
        self.mic_fails = 0
        for role in ("cable", "monitor"):
            if self.config[role] and not self._open_output(role, first):
                self.out_fails[role] = 1
                self.out_next[role] = now + _retry_delay(1)
        self._update_latency()

    def _make_output_callback(self, holder: dict):
        def callback(outdata, frames, _time, _status):
            fifo = holder.get("fifo")
            if fifo is None:
                outdata.fill(0)
                return
            try:
                outdata[:, :] = fifo.read(frames).reshape(-1, 1)
            except Exception:
                outdata.fill(0)
                self.cb_errors += 1
        return callback

    def _on_input(self, indata, _frames, _time, _status) -> None:
        self.last_callback = time.monotonic()
        try:
            mono = indata[:, 0] if indata.shape[1] == 1 else np.mean(indata, axis=1)
            proc = self.proc
            if proc is None:
                return
            self.silence_peak = max(self.silence_peak, float(np.max(np.abs(mono))) if len(mono) else 0.0)
            out = proc.process(mono)
            if len(out):
                cable, monitor = self.cable, self.monitor
                if cable is not None:
                    cable.fifo.write(out)
                if monitor is not None:
                    monitor.fifo.write(out)
        except Exception:
            self.cb_errors += 1
            log.exception("ошибка в обработке звука")

    # ---- управление ----

    def _stop_streams(self) -> None:
        streams = [self.input] + [o.stream for o in (self.cable, self.monitor) if o is not None]
        self.input = None
        self.cable = None
        self.monitor = None
        self.proc = None
        self.mic_silent = False
        self.latency_ms = 0.0
        for stream in streams:
            if stream is not None:
                self._close(stream)

    def _stop(self) -> None:
        self.running = False
        self.errors = {}
        self._stop_streams()

    @staticmethod
    def _active(stream) -> bool:
        try:
            return bool(stream.active)
        except Exception:
            return False

    def _watchdog(self) -> None:
        """Раз в секунду: если устройство отвалилось, подключаемся заново, с растущей паузой."""
        if not self.running or not self.config.get("mic"):
            return
        now = time.monotonic()
        mic_ok = self.input is not None and self._active(self.input) and now - self.last_callback < 2.5
        if not mic_ok:
            if now < self.next_retry:
                return
            if self.input is not None:
                log.warning("микрофон перестал присылать звук, перезапуск")
            fails = self.mic_fails
            config = dict(self.config)
            self._start(config["mic"], config["cable"], config["monitor"], config["fast"], first=self.input is not None or fails == 0)
            if self.input is None:
                self.mic_fails = fails + 1
                self.next_retry = now + _retry_delay(self.mic_fails)
            return
        for role in ("cable", "monitor"):
            if not self.config.get(role):
                continue
            output = getattr(self, role)
            if output is not None and self._active(output.stream):
                continue
            if now < self.out_next.get(role, 0.0):
                continue
            if output is not None:
                log.warning("%s остановился, перезапуск", ROLE_TITLES[role])
                setattr(self, role, None)
                self._close(output.stream)
            fails = self.out_fails.get(role, 0)
            if self._open_output(role, first=fails == 0):
                self.out_fails[role] = 0
                self._update_latency()
            else:
                self.out_fails[role] = fails + 1
                self.out_next[role] = now + _retry_delay(fails + 1)
        if now - self.started_at > 4.0:
            self.mic_silent = self.silence_peak < 1e-6
            self.started_at = now
            self.silence_peak = 0.0

    def _report(self) -> str:
        sd = self.sd
        lines = [f"PortAudio: {sd.get_portaudio_version()[1]}"]
        apis = sd.query_hostapis()
        lines.append("Драйверы: " + ", ".join(api["name"] for api in apis))
        lines.append("Устройства:")
        for d in self._all():
            lines.append(
                f"  {d['index']:>3} {d['host']}: {d['name']} "
                f"(вход {d['max_input_channels']}, выход {d['max_output_channels']}, {int(d['default_samplerate'])} Гц)"
            )
        lines.append(f"Выбрано: {self.config}")
        lines.append(f"Работает: микрофон {self.input is not None}, кабель {self.cable is not None}, "
                     f"слышать себя {self.monitor is not None}, задержка {round(self.latency_ms)} мс")
        for role, text in self.errors.items():
            lines.append(f"Ошибка ({role}): {text}")
        for role, attempts in self.attempts.items():
            if attempts:
                lines.append(f"Неудачные попытки ({role}):")
                lines.extend("  " + a for a in attempts[:40])
        return "\n".join(lines)
