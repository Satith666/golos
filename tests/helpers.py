import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths; paths.use(fakes=False)
import numpy as np
from scipy import signal
from dsp import VoiceProcessor

FS = 48000


def vowel(f0, dur=2.0, formants=((700, 110), (1200, 120), (2600, 170)), fs=FS):
    n = int(dur * fs)
    phase = np.cumsum(np.full(n, f0 / fs))
    x = np.diff(np.floor(phase), prepend=0.0)
    x = x - np.mean(x)
    x = signal.lfilter([1, -1], [1, -0.97], x)  # без постоянной составляющей
    x = signal.lfilter([1], [1, -0.9], x)
    for f, bw in formants:
        r = np.exp(-np.pi * bw / fs)
        x = signal.lfilter([1 - r], [1, -2 * r * np.cos(2 * np.pi * f / fs), r * r], x)
    x = x[fs // 4 :]
    x = x / np.max(np.abs(x)) * 0.5
    return x.astype(np.float32)


def run(p, x, fs=FS, fast=False, block=480):
    v = VoiceProcessor(fs, fast=fast)
    v.set_params(p)
    return np.concatenate([v.process(x[i : i + block]) for i in range(0, len(x), block)]), v


def formant_response(freqs, formants, stretch=1.0, fs=FS):
    w = 2 * np.pi * (freqs / stretch) / fs
    z = np.exp(1j * w)
    h = (1 - 1 / z) / (1 - 0.97 / z) / (1 - 0.9 / z)
    for f, bw in formants:
        r = np.exp(-np.pi * bw / fs)
        h = h * (1 - r) / (1 - 2 * r * np.cos(2 * np.pi * f / fs) / z + r * r / z ** 2)
    return 20 * np.log10(np.abs(h) + 1e-12)


def harmonic_score(y, f0, formants, stretch, fs=FS, top=3500.0):
    y = y[len(y) // 3 :]
    n = len(y)
    spec = np.abs(np.fft.rfft(y * np.hanning(n)))
    res = fs / n
    harmonics = np.arange(f0, top, f0)
    harmonics = harmonics[harmonics > 150.0]
    amp = []
    for h in harmonics:
        k = int(round(h / res))
        w = max(2, int(f0 * 0.2 / res))
        amp.append(np.max(spec[max(0, k - w) : k + w + 1]))
    db = 20 * np.log10(np.array(amp) + 1e-12)
    want = formant_response(harmonics, formants, stretch, fs)
    a = db - np.mean(db)
    b = want - np.mean(want)
    return float(np.corrcoef(a, b)[0, 1]), float(np.sqrt(np.mean((a - b) ** 2)))
