import sys, time
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths; paths.use(fakes=False)
import numpy as np
from scipy import signal
from dsp import VoiceProcessor, Params, FifoResampler

FS = 48000


def vowel(f0, dur=2.0, fs=FS, formants=((700, 110), (1200, 120), (2600, 170))):
    n = int(dur * fs)
    phase = np.cumsum(np.full(n, f0 / fs) * (1 + 0.004 * np.sin(2 * np.pi * 5 * np.arange(n) / fs)))
    pulse = np.diff(np.floor(phase), prepend=0.0)
    x = pulse
    x = signal.lfilter([1], [1, -0.97], x)
    for f, bw in formants:
        r = np.exp(-np.pi * bw / fs)
        a = [1, -2 * r * np.cos(2 * np.pi * f / fs), r * r]
        x = signal.lfilter([1 - r], a, x)
    x = x / np.max(np.abs(x)) * 0.5
    return x.astype(np.float32)


def run(p, x, fs=FS, fast=False, block=480):
    v = VoiceProcessor(fs, fast=fast)
    v.set_params(p)
    out = [v.process(x[i : i + block]) for i in range(0, len(x), block)]
    return np.concatenate(out), v


def f0_of(y, fs=FS):
    y = y[len(y) // 3 : len(y) // 3 + int(fs * 0.8)]
    y = y - np.mean(y)
    c = signal.fftconvolve(y, y[::-1])[len(y) - 1 :]
    lo, hi = int(fs / 500), int(fs / 45)
    lag = lo + int(np.argmax(c[lo:hi]))
    if 0 < lag < len(c) - 1:
        a, b, g = c[lag - 1], c[lag], c[lag + 1]
        lag = lag + 0.5 * (a - g) / (a - 2 * b + g)
    return fs / lag


def centroid(y, fs=FS, band=4000):
    f, p = signal.welch(y[len(y) // 3 :], fs, nperseg=4096)
    m = f < band
    return float(np.sum(f[m] * p[m]) / np.sum(p[m]))


print("== точность высоты (f0 исходный 130 Гц)")
x = vowel(130)
base_f0 = f0_of(x)
print(f"исходный f0 = {base_f0:.1f}")
bad = 0
for st in (-12, -9, -7, -4, -2, 2, 4, 7, 12):
    y, _ = run(Params(pitch=st, gate_db=-80), x)
    got = 12 * np.log2(f0_of(y) / base_f0)
    # октавная ошибка автокорреляции не считается ошибкой сдвига
    print(f"сдвиг {st:+4d}: измерено {got:+6.2f} полутона, пик {np.max(np.abs(y)):.2f}")
    if abs(got - st) > 0.5:
        bad += 1
print("ошибок >0.5 полутона:", bad)
assert bad == 0

print("\n== форманты: центр тяжести спектра (до 4 кГц)")
print(f"исходный: {centroid(x):.0f} Гц")
for st, fm in ((-7, 0), (-7, -7), (7, 0), (7, 7), (0, 6), (0, -6)):
    y, _ = run(Params(pitch=st, formant=fm, gate_db=-80), x)
    print(f"pitch {st:+d} formant {fm:+d}: {centroid(y):.0f} Гц")

print("\n== прозрачность при нуле")
y, v = run(Params(gate_db=-80), x)
d = v.n
a, b = x[10000:60000], y[10000 + d - v.hop:60000 + d - v.hop]
best = min((np.sqrt(np.mean((x[10000:60000] - y[10000 + s:60000 + s]) ** 2)), s) for s in range(0, 3000, 1))
print(f"лучшее совпадение: сдвиг {best[1]} отсчётов = {1000*best[1]/FS:.1f} мс, ошибка {20*np.log10(best[0]/np.sqrt(np.mean(x[10000:60000]**2))):.1f} дБ")

print("\n== скорость (один шаг hop, все эффекты)")
for fast in (False, True):
    p = Params(pitch=-7, formant=-3, robot=0.2, whisper=0.1, metal=0.2, drive=0.3, radio=0.2, echo=0.3, reverb=0.3, denoise=True)
    v = VoiceProcessor(FS, fast=fast)
    v.set_params(p)
    t = []
    data = vowel(130, dur=2.0)
    for i in range(0, len(data) - v.hop, v.hop):
        s = time.perf_counter()
        v.process(data[i : i + v.hop])
        t.append(time.perf_counter() - s)
    t = np.array(t[10:]) * 1000
    budget = 1000 * v.hop / FS
    print(f"fast={fast}: N={v.n} hop={v.hop} задержка {v.latency_ms:.1f} мс, бюджет {budget:.1f} мс, среднее {t.mean():.2f} мс, максимум {t.max():.2f} мс, загрузка {100*t.mean()/budget:.0f}%")

print("\n== пресеты на устойчивость")
from presets import BUILTIN
for name, cfg in BUILTIN.items():
    y, _ = run(Params.from_dict(cfg), x)
    print(f"{name:18s} пик {np.max(np.abs(y)):.3f}  rms {20*np.log10(np.sqrt(np.mean(y**2))+1e-9):6.1f} дБ  nan {int(np.isnan(y).sum())}")
    assert not np.isnan(y).any() and np.max(np.abs(y)) <= 1.0

print("\n== частоты дискретизации и размеры блоков")
for fs in (8000, 16000, 44100, 48000, 96000):
    xx = vowel(130, dur=1.0, fs=fs)
    for block in (1, 37, 256, 4096):
        y, v = run(Params(pitch=-5, formant=-2, reverb=0.3, echo=0.3, gate_db=-80), xx[:fs//2 if block == 1 else None], fs=fs, block=block)
        assert np.isfinite(y).all()
    print(f"{fs}: ok N={v.n} hop={v.hop}")

print("\n== гейт и тишина")
noise = (np.random.default_rng(1).normal(size=FS * 2) * 10 ** (-70 / 20)).astype(np.float32)
y, _ = run(Params(gate_db=-55), noise)
print(f"шум -70 дБ через гейт -55: rms {20*np.log10(np.sqrt(np.mean(y[FS:]**2))+1e-12):.1f} дБ")
y, _ = run(Params(mute=True), x)
print(f"mute: пик {np.max(np.abs(y)):.6f}")
y, _ = run(Params(input_db=24, gate_db=-80), x)
print(f"+24 дБ на входе, лимитер: пик {np.max(np.abs(y)):.3f}")

print("\n== шумодав")
rng = np.random.default_rng(2)
noise = (rng.normal(size=len(x)) * 10 ** (-40 / 20)).astype(np.float32)
sig = np.concatenate((noise[:FS], (x + noise)[:FS * 2 // 1]))
y0, _ = run(Params(gate_db=-80, denoise=False), noise)
y1, _ = run(Params(gate_db=-80, denoise=True), noise)
print(f"чистый шум: без шумодава {20*np.log10(np.sqrt(np.mean(y0[FS//2:]**2))):.1f} дБ, с шумодавом {20*np.log10(np.sqrt(np.mean(y1[FS//2:]**2))):.1f} дБ")
yv, _ = run(Params(gate_db=-80, denoise=True), x + noise)
print(f"голос с шумодавом: rms {20*np.log10(np.sqrt(np.mean(yv[FS//2:]**2))):.1f} дБ, исходный {20*np.log10(np.sqrt(np.mean(x[FS//2:]**2))):.1f} дБ")

print("\n== очередь и рассинхрон часов")
for out_rate, drift in ((48000, 1.0), (48000, 1.0005), (48000, 0.9995), (44100, 1.0), (96000, 1.0)):
    q = FifoResampler(48000, out_rate * drift, target_ms=30)
    t_in, t_out = 0.0, 0.0
    fills, phase = [], 0.0
    produced = []
    tone = lambda n, ph: (0.5 * np.sin(2 * np.pi * 440 * (ph + np.arange(n)) / 48000)).astype(np.float32)
    in_block, out_block = 512, 480
    pos = 0
    dt_in, dt_out = in_block / 48000, out_block / (out_rate * drift)
    total = 60.0
    while min(t_in, t_out) < total:
        if t_in <= t_out:
            q.write(tone(in_block, pos)); pos += in_block; t_in += dt_in
        else:
            produced.append(q.read(out_block)); t_out += dt_out; fills.append(q.fill())
    f = np.array(fills[len(fills) // 4 :]) / 48
    audio = np.concatenate(produced)
    tail = audio[len(audio) // 2 :]
    d = np.diff(tail, 2)
    print(f"выход {out_rate} дрейф {drift}: очередь {f.min():.1f}..{f.max():.1f} мс, срывы {q.underruns}, сбросы {q.skips}, макс. скачок {np.max(np.abs(d)):.4f}")
