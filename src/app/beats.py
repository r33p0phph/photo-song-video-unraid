"""Find the beat of a song, so that picture cuts can land on it.

Plain numpy, no extra models: spectral flux gives an onset curve, autocorrelation gives the tempo,
and the best-fitting grid gives the beat times. It suits songs with a steady beat. When the song has
none, detect() returns None and the caller falls back to a fixed cut length.
"""
import subprocess

import numpy as np

SR = 22050
NFFT = 1024
HOP = 512
FPS = SR / HOP
MAX_ANALYSE_SECONDS = 900      # more than this is not needed to find a steady tempo
BPM_LOW, BPM_HIGH = 70.0, 170.0
MIN_RATIO = 1.7                 # onset strength on the beats, against the average


def _decode(path: str, start: float, length: float):
    length = min(length, MAX_ANALYSE_SECONDS)
    p = subprocess.run(
        ["ffmpeg", "-v", "error", "-protocol_whitelist", "file", "-vn", "-ss", "%.3f" % start, "-t", "%.3f" % length,
         "-i", path, "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"],
        capture_output=True, timeout=300)
    if p.returncode != 0:
        return None
    return np.frombuffer(p.stdout, dtype=np.float32)


def _onset_curves(x: np.ndarray):
    """Onset strength over time: the whole spectrum, and the low end (kick drums) on its own."""
    n = 1 + (len(x) - NFFT) // HOP
    win = np.hanning(NFFT).astype(np.float32)
    prev = None
    full = np.zeros(n, dtype=np.float32)
    low = np.zeros(n, dtype=np.float32)
    chunk = 1500
    for a in range(0, n, chunk):
        b = min(n, a + chunk)
        idx = np.arange(NFFT)[None, :] + HOP * np.arange(a, b)[:, None]
        mag = np.log1p(np.abs(np.fft.rfft(x[idx] * win, axis=1))[:, 1:300] * 20.0)   # about 20 Hz to 6 kHz
        stack = mag if prev is None else np.vstack([prev, mag])
        d = np.maximum(0, np.diff(stack, axis=0))
        off = 1 if prev is None else 0
        full[a + off:b] = d.sum(axis=1)
        low[a + off:b] = d[:, :12].sum(axis=1)                                      # up to about 250 Hz
        prev = mag[-1:]
    k = int(FPS * 0.5)
    box = np.ones(k) / k
    return (np.maximum(0, full - np.convolve(full, box, mode="same")),
            np.maximum(0, low - np.convolve(low, box, mode="same")))


def _grid(sm: np.ndarray, lag: float):
    """Best phase for beats every `lag` frames: returns (phase, mean onset strength on the beats)."""
    n = len(sm)
    best_ph, best_v = 0.0, -1.0
    for ph in np.arange(0.0, lag, 0.5):
        pos = np.round(np.arange(ph, n - 1, lag)).astype(int)
        v = float(sm[pos].mean())
        if v > best_v:
            best_ph, best_v = float(ph), v
    return best_ph, best_v


def _refine(sm: np.ndarray, phase: float, lag: float):
    """Snap each beat to the nearest strong onset and fit a straight line, so the tempo is exact over minutes."""
    n = len(sm)
    for _ in range(2):
        k = np.arange(0, int((n - 1 - phase) // lag))
        pos = phase + k * lag
        w = int(max(2, lag * 0.2))
        xs, ys = [], []
        for ki, p in zip(k, pos):
            a, b = int(max(0, round(p) - w)), int(min(n, round(p) + w + 1))
            if b - a < 3:
                continue
            j = a + int(np.argmax(sm[a:b]))
            if sm[j] > 0:
                xs.append(ki)
                ys.append(j)
        if len(xs) < 8:
            break
        slope, icpt = np.polyfit(xs, ys, 1)
        if abs(slope - lag) > lag * 0.05:       # the fit disagrees too much: keep the grid as it was
            break
        lag, phase = float(slope), float(icpt)
    while phase < 0:
        phase += lag
    return phase, lag


def detect(path: str, start: float, length: float):
    """Return {"bpm", "beats": [seconds from start, ...]} or None when there is no clear beat."""
    x = _decode(path, start, length)
    if x is None or len(x) < SR * 6:
        return None
    full, low = _onset_curves(x)
    if full.max() <= 0:
        return None
    full = full / full.max()
    low = low / (low.max() + 1e-9)
    sm = np.maximum.reduce([full, np.roll(full, 1), np.roll(full, -1)]) + 2.0 * np.maximum.reduce(
        [low, np.roll(low, 1), np.roll(low, -1)])

    n = len(full)
    f = np.fft.rfft(full - full.mean(), 2 * n)
    ac = np.fft.irfft(f * np.conj(f))[:n]
    lo, hi = int(FPS * 60.0 / BPM_HIGH), int(FPS * 60.0 / BPM_LOW)
    lags = np.arange(lo, hi + 1)
    prior = lambda bpm: np.exp(-0.5 * (np.log2(bpm / 120.0) / 0.6) ** 2)
    score = ac[lags] * prior(60.0 * FPS / lags)

    # candidates: the strongest autocorrelation peaks, each also halved and doubled
    peaks = [int(lags[j]) for j in np.argsort(score)[::-1]]
    cands = []
    for lag in peaks:
        if all(abs(lag - c) > 2 for c in cands):
            cands.append(lag)
        if len(cands) >= 5:
            break
    extra = []
    for c in cands:
        for m in (0.5, 2.0):
            if lo <= c * m <= hi:
                extra.append(int(round(c * m)))
    cands = list(dict.fromkeys(cands + extra))

    mean_all = float(sm.mean()) + 1e-9
    best = None
    for lag0 in cands:
        # the autocorrelation lag is a whole number of frames, so try tempos a little either side too:
        # a tiny tempo error would drift off the beat over a few minutes
        for lag in np.linspace(lag0 * 0.96, lag0 * 1.04, 41):
            ph, v = _grid(sm, float(lag))
            s_ = (v / mean_all) * (0.4 + prior(60.0 * FPS / lag))
            if best is None or s_ > best[0]:
                best = (s_, ph, float(lag), v / mean_all)
    _, phase, lag, ratio = best
    if ratio < MIN_RATIO:                 # beats are not clearly stronger than the rest of the music
        return None
    phase, lag = _refine(sm, phase, lag)
    pos = np.arange(phase, n - 1, lag)
    # the onset curve is stamped at the start of its window, so add about 48 ms to land on the sound itself
    delay = (NFFT / 2 + HOP) / SR
    return {"bpm": 60.0 * FPS / lag, "beats": [float(p / FPS) + delay for p in pos],
            "confidence": ratio}
