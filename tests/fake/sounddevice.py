"""Имитация sounddevice: те же вызовы, что у настоящей библиотеки, звук идёт в реальном времени."""
import threading, time
import numpy as np

class PortAudioError(Exception): pass
class WasapiSettings:
    def __init__(self, exclusive=False, auto_convert=False, explicit_sample_format=False):
        self.auto_convert = auto_convert

STATE = {"cable": True, "fail_first": 0, "kill": None, "silent": False,
         "fail_open": {}, "fail_start": {}, "wrong_thread": []}
_init_thread = threading.get_ident()
import os as _os
if _os.environ.get("FAKE_FAIL_OPEN"):
    _n, _h, _m = _os.environ["FAKE_FAIL_OPEN"].split("|", 2)
    STATE["fail_open"][(_n, _h)] = _m
START_ERROR = ("Error starting stream: Unanticipated host error [PaErrorCode -9999]: 'WdmSyncIoctl: DeviceIoControl GLE = 0x00000490 "
               "(prop_set = {8C134960-51AD-11CF-878A-94F801C10000}, prop_id = 10)' [Windows WDM-KS error 0]")

def _check_thread(what):
    if threading.get_ident() != _init_thread:
        STATE["wrong_thread"].append((what, threading.current_thread().name))
        return False
    return True
__version__ = "fake"
def get_portaudio_version(): return (1, "fake portaudio")

def _devs():
    d = [
      ("Микрофон (Realtek(R) Audio)", 1, 2, 0, 44100.0),
      ("Динамики (Realtek(R) Audio)", 1, 0, 2, 48000.0),
      ("Наушники (HyperX Cloud II Wireless)", 1, 0, 2, 48000.0),
      ("Микрофон (Realtek(R) Audio)", 0, 2, 0, 44100.0),
      ("Микрофон (USB Camera)", 1, 1, 0, 48000.0),
    ]
    if STATE["cable"]:
        d += [("CABLE Output (VB-Audio Virtual Cable)", 1, 2, 0, 48000.0), ("CABLE Input (VB-Audio Virtual Cable)", 1, 0, 2, 48000.0)]
    return [dict(name=n, hostapi=h, max_input_channels=i, max_output_channels=o, default_samplerate=r) for n, h, i, o, r in d]

def query_devices():
    _check_thread("query_devices"); return _devs()
def query_hostapis():
    _check_thread("query_hostapis")
    return [dict(name="MME", default_input_device=3, default_output_device=1), dict(name="Windows WASAPI", default_input_device=0, default_output_device=1)]
def _terminate():
    _check_thread("terminate")
def _initialize():
    global _init_thread
    _init_thread = threading.get_ident()

class _Stream:
    def __init__(self, device, samplerate, channels, dtype, blocksize, latency, callback, extra_settings):
        _check_thread("open")
        info = _devs()[device]
        host = query_hostapis()[info["hostapi"]]["name"]
        if (info["name"], host) in STATE["fail_open"]:
            raise PortAudioError(STATE["fail_open"][(info["name"], host)])
        self.host = host
        if STATE["fail_first"] > 0:
            STATE["fail_first"] -= 1
            raise PortAudioError("Invalid sample rate")
        assert dtype == "float32" and blocksize == 0 and latency == "low"
        assert extra_settings is None or extra_settings.auto_convert
        self.info, self.rate, self.ch, self.cb = info, samplerate, channels, callback
        self.active = False; self.latency = 0.012; self.closed = False; self.t = None; self.blocks = 0
    def start(self):
        # как WASAPI в Windows: запуск из потока без COM падает с устаревшим текстом ошибки
        if not _check_thread("start"):
            raise PortAudioError(START_ERROR)
        if (self.info["name"], self.host) in STATE["fail_start"]:
            raise PortAudioError(STATE["fail_start"][(self.info["name"], self.host)])
        self.active = True
        self.t = threading.Thread(target=self._loop, daemon=True); self.t.start()
    def abort(self):
        _check_thread("abort"); self.active = False
    def close(self):
        _check_thread("close"); self.closed = True
    def _loop(self):
        n = int(self.rate * 0.01); next_t = time.perf_counter(); pos = 0
        while self.active:
            if STATE["kill"] == self.info["name"]:
                STATE["kill"] = None; self.active = False; return
            self.step(n, pos); pos += n; self.blocks += 1
            next_t += n / self.rate
            d = next_t - time.perf_counter()
            if d > 0: time.sleep(d)

class InputStream(_Stream):
    def step(self, n, pos):
        t = (pos + np.arange(n)) / self.rate
        y = 0.25 * np.sin(2*np.pi*150*t) * (np.sin(2*np.pi*1.5*t) > -0.4)
        if STATE["silent"]: y = y * 0
        self.cb(np.repeat(y.astype(np.float32)[:, None], self.ch, 1), n, None, None)

CAPTURE = {}
class OutputStream(_Stream):
    def step(self, n, pos):
        o = np.ones((n, self.ch), dtype=np.float32)
        self.cb(o, n, None, None)
        CAPTURE.setdefault(self.info["name"], []).append(o[:, 0].copy())
