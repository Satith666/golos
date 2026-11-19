"""Обработка голоса: высота, тембр, эффекты. Только numpy, ничего не знает про звуковые карты."""

from __future__ import annotations

import threading
from dataclasses import dataclass, asdict, fields

import numpy as np

EPS = 1e-9


@dataclass(frozen=True)
class Params:
    pitch: float = 0.0        # высота, полутоны
    formant: float = 0.0      # тембр, полутоны
    robot: float = 0.0        # 0..1
    whisper: float = 0.0      # 0..1
    metal: float = 0.0        # 0..1
    metal_hz: float = 70.0    # частота «металла», Гц
    drive: float = 0.0        # искажение 0..1
    radio: float = 0.0        # рация 0..1
    echo: float = 0.0         # эхо 0..1
    reverb: float = 0.0       # реверберация 0..1
    gate_db: float = -55.0    # порог шумового гейта, дБ (-80 = выключен)
    denoise: bool = False     # шумоподавление
    input_db: float = 0.0     # усиление микрофона, дБ
    output_db: float = 0.0    # громкость на выходе, дБ
    trim_db: float = 0.0      # поправка громкости пресета, дБ
    bypass: bool = False      # True — голос без изменений
    mute: bool = False        # True — тишина

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Params":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


def _db(x: float) -> float:
    return 10.0 ** (x / 20.0)


class _Delay:
    """Линия задержки, работающая блоками. Длина задержки всегда не меньше блока."""

    def __init__(self, size: int):
        self.buf = np.zeros(size, dtype=np.float32)
        self.size = size
        self.w = 0

    def read(self, delay: int, n: int, offset: int = 0) -> np.ndarray:
        idx = (self.w - delay - offset + np.arange(n)) % self.size
        return self.buf[idx]

    def write(self, x: np.ndarray) -> None:
        idx = (self.w + np.arange(len(x))) % self.size
        self.buf[idx] = x
        self.w = (self.w + len(x)) % self.size


class _Reverb:
    """Реверберация по схеме Шрёдера/Freeverb. Задержки длиннее блока, поэтому считается векторно."""

    COMBS = (1116, 1188, 1277, 1356, 1422, 1491, 1557, 1617)
    ALLPASS = (556, 441)

    def __init__(self, fs: int, block: int):
        scale = fs / 44100.0
        self.comb_d = [max(block + 2, int(d * scale)) for d in self.COMBS]
        self.ap_d = [max(block + 2, int(d * scale)) for d in self.ALLPASS]
        self.combs = [_Delay(d + block + 4) for d in self.comb_d]
        self.aps = [_Delay(d + block + 4) for d in self.ap_d]

    def process(self, x: np.ndarray) -> np.ndarray:
        n = len(x)
        out = np.zeros(n, dtype=np.float32)
        for line, d in zip(self.combs, self.comb_d):
            a = line.read(d, n)
            b = line.read(d, n, offset=1)
            y = 0.5 * (a + b)  # лёгкое затухание верхов
            out += y
            line.write(x * 0.25 + y * 0.84)
        for line, d in zip(self.aps, self.ap_d):
            delayed = line.read(d, n)
            y = delayed - 0.5 * out
            line.write(out + 0.5 * delayed)
            out = y
        return out * 0.5


class VoiceProcessor:
    """Принимает блоки любой длины, отдаёт обработанный звук порциями по hop отсчётов."""

    ENV_MS = 2.0
    ENV_ITERS = 6
    NOISE_UP = 0.02
    NOISE_SLOW = 0.0006
    NOISE_DOWN = 0.12
    NOISE_OVER = 3.0

    def __init__(self, fs: int, fast: bool = False):
        self.fs = int(fs)
        target = 0.0213 if fast else 0.0427
        self.n = int(2 ** round(np.log2(max(256.0, target * self.fs))))
        self.hop = self.n // 4
        self.bins = self.n // 2 + 1
        self.params = Params()

        self.window = np.hanning(self.n + 1)[:-1].astype(np.float64)
        self.norm = 1.0 / 1.5
        self.in_pending = np.zeros(0, dtype=np.float32)
        self.frame = np.zeros(self.n, dtype=np.float64)
        self.ola = np.zeros(self.n, dtype=np.float64)

        self.k = np.arange(self.bins, dtype=np.float64)
        self.omega = 2.0 * np.pi * self.k / self.n
        self.prev_phase = np.zeros(self.bins)
        self.theta = np.zeros(self.bins)
        self.sign = np.where(np.arange(self.bins) % 2 == 0, 1.0, -1.0)

        # окно кепстра для огибающей голоса
        cut = max(8, int(self.ENV_MS * 0.001 * self.fs))
        cut = min(cut, self.n // 4)
        lifter = np.zeros(self.n)
        lifter[:cut] = 1.0
        taper = np.hanning(2 * max(2, cut // 3) + 1)
        tail = len(taper) // 2
        lifter[cut:cut + tail] = taper[tail + 1:][:len(lifter[cut:cut + tail])]
        lifter[1:self.n // 2] *= 2.0
        lifter[self.n // 2 + 1:] = 0.0
        self.lifter = lifter

        f = self.k * self.fs / self.n
        self.hipass = np.clip((f - 45.0) / 45.0, 0.0, 1.0) ** 2
        low = np.clip((f - 280.0) / 170.0, 0.0, 1.0)
        high = np.clip((3400.0 - f) / 700.0, 0.0, 1.0)
        self.radio_mask = low * high

        self.noise = None
        self.smooth_power = None
        self.ns_gain = np.ones(self.bins)

        self.gate_gain = 1.0
        self.gate_open = False
        self.gate_hold = 0
        self.hold_blocks = max(1, int(0.18 * self.fs / self.hop))
        self.gate_release = 1.0 - min(1.0, self.hop / (0.09 * self.fs))

        self.ring_phase = 0.0
        self.echo_d = int(0.29 * self.fs)
        self.echo = _Delay(self.echo_d + self.hop + 4)
        self.reverb = _Reverb(self.fs, self.hop)
        self.lim_gain = 1.0
        self.rng = np.random.default_rng()

        self.in_peak = 0.0
        self.out_peak = 0.0
        self.input_rms_db = -120.0
        # последние ~20 мс звука для живой волны в окне программы
        self.scope_len = max(256, int(0.022 * self.fs))
        self.scope_in = np.zeros(self.scope_len, dtype=np.float32)
        self.scope_out = np.zeros(self.scope_len, dtype=np.float32)

    @property
    def latency_ms(self) -> float:
        return 1000.0 * self.n / self.fs

    def set_params(self, params: Params) -> None:
        self.params = params

    def take_scope(self, points: int = 160) -> tuple[list, list]:
        index = np.linspace(0, self.scope_len - 1, points).astype(np.int64)
        a = np.round(self.scope_in[index].astype(np.float64), 3).tolist()
        b = np.round(self.scope_out[index].astype(np.float64), 3).tolist()
        return a, b

    @staticmethod
    def _push(buffer: np.ndarray, data: np.ndarray) -> None:
        n = len(data)
        if n >= len(buffer):
            buffer[:] = data[-len(buffer):]
        else:
            buffer[:-n] = buffer[n:]
            buffer[-n:] = data

    def take_levels(self) -> tuple[float, float]:
        a, b = self.in_peak, self.out_peak
        self.in_peak = 0.0
        self.out_peak = 0.0
        return a, b

    def _gate(self, x: np.ndarray, p: Params) -> np.ndarray:
        rms = float(np.sqrt(np.mean(x * x) + EPS * EPS))
        level = 20.0 * np.log10(rms + EPS)
        self.input_rms_db = level
        if p.gate_db <= -79.5:
            target = 1.0
            self.gate_open = True
        else:
            if level >= p.gate_db:
                self.gate_open = True
                self.gate_hold = self.hold_blocks
            elif level < p.gate_db - 5.0:
                if self.gate_hold > 0:
                    self.gate_hold -= 1
                else:
                    self.gate_open = False
            target = 1.0 if self.gate_open else 0.0
        new = target if target > self.gate_gain else max(target, self.gate_gain * self.gate_release)
        if new < 0.0005:
            new = 0.0
        ramp = np.linspace(self.gate_gain, new, len(x), endpoint=False, dtype=np.float32)
        self.gate_gain = new
        return x * ramp

    def _denoise(self, X: np.ndarray) -> np.ndarray:
        power = (X.real ** 2 + X.imag ** 2) + 1e-14
        if self.noise is None:
            self.noise = power.copy()
            self.smooth_power = power.copy()
        self.smooth_power = 0.75 * self.smooth_power + 0.25 * power
        diff = self.smooth_power - self.noise
        # когда уровень намного выше шума, это голос: оценку шума почти не трогаем
        speech = self.smooth_power > self.noise * 5.0
        rate = np.where(diff <= 0.0, self.NOISE_DOWN, np.where(speech, self.NOISE_SLOW, self.NOISE_UP))
        self.noise += rate * diff
        wanted = np.clip(1.0 - self.NOISE_OVER * self.noise / self.smooth_power, 0.1, 1.0)
        self.ns_gain = 0.6 * self.ns_gain + 0.4 * wanted
        return X * self.ns_gain

    def _shift(self, X: np.ndarray, beta: float) -> np.ndarray:
        """Сдвиг высоты с привязкой фаз к пикам спектра (метод Ларош-Долсона)."""
        mag = np.abs(X)
        phase = np.angle(X)
        hop = self.hop
        m = mag[2:-2]
        mask = (m > mag[1:-3]) & (m > mag[3:-1]) & (m > mag[:-4]) & (m > mag[4:]) & (m > 1e-7)
        peaks = np.nonzero(mask)[0] + 2
        out = np.zeros(self.bins, dtype=np.complex128)
        if len(peaks) == 0:
            self.prev_phase = phase
            self.theta[:] = 0.0
            return out

        delta = phase[peaks] - self.prev_phase[peaks] - self.omega[peaks] * hop
        delta -= 2.0 * np.pi * np.round(delta / (2.0 * np.pi))
        inst = self.omega[peaks] + delta / hop
        theta = self.theta[peaks] + (beta - 1.0) * inst * hop
        theta -= 2.0 * np.pi * np.round(theta / (2.0 * np.pi))

        edges = (peaks[:-1] + peaks[1:]) // 2 + 1
        region = np.searchsorted(edges, np.arange(self.bins), side="right")
        shift = np.rint(peaks * (beta - 1.0)).astype(np.int64)

        bin_theta = theta[region]
        target = np.arange(self.bins) + shift[region]
        keep = (target >= 0) & (target < self.bins)
        np.add.at(out, target[keep], X[keep] * np.exp(1j * bin_theta[keep]))

        self.theta = bin_theta
        self.prev_phase = phase
        return out

    def _log_envelope(self, X: np.ndarray) -> np.ndarray:
        """Огибающая спектра методом true envelope: она идёт по вершинам гармоник, а не между ними."""
        target = np.log(np.abs(X) + 1e-7)
        env = self._cepstral_smooth(target)
        for _ in range(self.ENV_ITERS):
            target = np.maximum(target, env)
            env = self._cepstral_smooth(target)
        return env

    def _cepstral_smooth(self, log_mag: np.ndarray) -> np.ndarray:
        cep = np.fft.irfft(log_mag, self.n)
        return np.fft.rfft(cep * self.lifter, self.n).real

    def _frame(self, p: Params) -> np.ndarray:
        X = np.fft.rfft(self.frame * self.window)
        if p.bypass:
            self.prev_phase = np.angle(X)
            self.theta[:] = 0.0
            Y = X * self.hipass
        else:
            if p.denoise:
                X = self._denoise(X)
            X = X * self.hipass
            beta = 2.0 ** (p.pitch / 12.0)
            gamma = 2.0 ** (p.formant / 12.0)
            need_env = abs(beta - gamma) > 1e-4
            env = self._log_envelope(X) if need_env else None
            if abs(beta - 1.0) > 1e-4:
                Y = self._shift(X, beta)
            else:
                self.prev_phase = np.angle(X)
                self.theta[:] = 0.0
                Y = X
            if need_env:
                wanted = np.interp(self.k / gamma, self.k, env)
                current = np.interp(self.k / beta, self.k, env)
                gain = np.exp(np.clip(wanted - current, -3.0, 2.2))
                Y = Y * gain
            if p.radio > 0.0:
                Y = Y * ((1.0 - p.radio) + p.radio * self.radio_mask)
            if p.robot > 0.0:
                Y = (1.0 - p.robot) * Y + p.robot * np.abs(Y) * self.sign * 1.6
            if p.whisper > 0.0:
                random = np.exp(2j * np.pi * self.rng.random(self.bins))
                Y = (1.0 - p.whisper) * Y + p.whisper * np.abs(Y) * random * 1.25
        y = np.fft.irfft(Y, self.n) * self.window * self.norm
        self.ola += y
        out = self.ola[: self.hop].copy()
        self.ola[:-self.hop] = self.ola[self.hop:]
        self.ola[-self.hop:] = 0.0
        return out.astype(np.float32)

    def _post(self, y: np.ndarray, p: Params) -> np.ndarray:
        if not p.bypass:
            drive = p.drive + 0.35 * p.radio
            if drive > 0.0:
                d = 1.0 + 14.0 * min(drive, 1.0)
                y = np.tanh(y * d) / np.tanh(d) / (1.0 + 0.3 * (d - 1.0) ** 0.5)
            if p.metal > 0.0:
                step = 2.0 * np.pi * p.metal_hz / self.fs
                t = self.ring_phase + step * np.arange(len(y))
                self.ring_phase = float((self.ring_phase + step * len(y)) % (2.0 * np.pi))
                y = y * ((1.0 - p.metal) + p.metal * np.sin(t).astype(np.float32) * 1.4)
            if p.echo > 0.0:
                delayed = self.echo.read(self.echo_d, len(y))
                self.echo.write(y + delayed * 0.45)
                y = y + delayed * p.echo * 0.8
            else:
                self.echo.write(y * 0.0)
            if p.reverb > 0.0:
                y = y * (1.0 - 0.35 * p.reverb) + self.reverb.process(y) * p.reverb * 1.3
        y = y * _db(p.output_db + (0.0 if p.bypass else p.trim_db))
        if p.mute:
            y = y * 0.0
        peak = float(np.max(np.abs(y))) if len(y) else 0.0
        limit = 0.95
        target = 1.0 if peak <= limit else limit / peak
        if target < self.lim_gain:
            new = target
        else:
            new = min(target, self.lim_gain + (1.0 - self.lim_gain) * 0.06)
        ramp = np.linspace(self.lim_gain, new, len(y), endpoint=True, dtype=np.float32)
        self.lim_gain = new
        y = y * ramp
        y = np.clip(y, -0.999, 0.999)
        return y

    def process(self, block: np.ndarray) -> np.ndarray:
        """Вернуть всё, что удалось обработать. Длина ответа кратна hop (может быть 0)."""
        p = self.params
        block = np.nan_to_num(np.asarray(block, dtype=np.float32), nan=0.0, posinf=0.0, neginf=0.0)
        data = np.concatenate((self.in_pending, block)) if len(self.in_pending) else block
        count = len(data) // self.hop
        if count == 0:
            self.in_pending = data.copy()
            return np.zeros(0, dtype=np.float32)
        chunks = []
        for i in range(count):
            piece = data[i * self.hop : (i + 1) * self.hop] * _db(p.input_db)
            self.in_peak = max(self.in_peak, float(np.max(np.abs(piece))))
            self._push(self.scope_in, piece)
            piece = self._gate(piece, p)
            self.frame[:-self.hop] = self.frame[self.hop:]
            self.frame[-self.hop:] = piece
            out = self._post(self._frame(p), p)
            self.out_peak = max(self.out_peak, float(np.max(np.abs(out))))
            self._push(self.scope_out, out)
            chunks.append(out)
        self.in_pending = data[count * self.hop :].copy()
        return np.concatenate(chunks) if len(chunks) > 1 else chunks[0]


class FifoResampler:
    """Очередь между потоками ввода и вывода.

    У микрофона и виртуального кабеля разные часы и иногда разная частота. Вывод читает очередь
    с небольшой поправкой скорости (до 1 %), поэтому очередь не растёт и не пустеет, а щелчков нет.
    """

    def __init__(self, input_rate: float, output_rate: float, target_ms: float):
        self.base = float(input_rate) / float(output_rate)
        self.size = int(input_rate * 2)
        self.buf = np.zeros(self.size, dtype=np.float32)
        self.target = input_rate * target_ms / 1000.0
        self.limit = input_rate * (target_ms + 120.0) / 1000.0
        self.w = 0
        self.r = 2
        self.frac = 0.0
        self.avg = self.target
        self.primed = False
        self.underruns = 0
        self.skips = 0
        self.lock = threading.Lock()

    def fill(self) -> int:
        return self.w - self.r

    def write(self, x: np.ndarray) -> None:
        with self.lock:
            self._write(x)

    def read(self, n: int) -> np.ndarray:
        with self.lock:
            return self._read(n)

    def _write(self, x: np.ndarray) -> None:
        n = len(x)
        if n == 0:
            return
        idx = (self.w + np.arange(n)) % self.size
        self.buf[idx] = x
        self.w += n
        if self.w - self.r > self.size - 8:
            self.r = self.w - int(self.target)
            self.frac = 0.0
            self.skips += 1

    def _read(self, n: int) -> np.ndarray:
        out = np.zeros(n, dtype=np.float32)
        fill = self.fill()
        if not self.primed:
            if fill >= self.target:
                self.primed = True
                self.avg = float(fill)
            else:
                return out
        self.avg += 0.03 * (fill - self.avg)
        if self.avg > self.limit:
            self.r = self.w - int(self.target)
            self.frac = 0.0
            self.avg = self.target
            self.skips += 1
            fill = self.fill()
        err = (self.avg - self.target) / max(1.0, self.target)
        adjust = float(np.clip(err * 0.02, -0.01, 0.01))
        rate = self.base * (1.0 + adjust)

        pos = self.frac + rate * np.arange(n)
        last = int(pos[-1]) + 3
        if last >= fill:
            usable = int(np.searchsorted(pos, fill - 3.0))
            self.underruns += 1
            self.primed = False
            if usable <= 0:
                return out
            pos = pos[:usable]
        i = pos.astype(np.int64)
        t = (pos - i).astype(np.float32)
        y0 = self.buf[(self.r + i - 1) % self.size]
        y1 = self.buf[(self.r + i) % self.size]
        y2 = self.buf[(self.r + i + 1) % self.size]
        y3 = self.buf[(self.r + i + 2) % self.size]
        out[: len(pos)] = y1 + 0.5 * t * (
            y2 - y0 + t * (2.0 * y0 - 5.0 * y1 + 4.0 * y2 - y3 + t * (3.0 * (y1 - y2) + y3 - y0))
        )
        advance = self.frac + rate * len(pos)
        whole = int(advance)
        self.r += whole
        self.frac = advance - whole
        return out
