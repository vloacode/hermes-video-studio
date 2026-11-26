"""video_studio.music - tempo y beats de una cancion (FFmpeg + NumPy, sin librosa).

Envolvente de ataques (flujo espectral) -> tempo por autocorrelacion (70-180 BPM,
preferencia suave por ~120) -> fase del primer beat. Suficiente para cortar a ritmo.
"""
import subprocess

import numpy as np

SR, N, HOP = 11025, 1024, 256
LAT = 0.078   # correccion de latencia del detector (s), calibrada con un metronomo


def onset_envelope(path, max_sec=150):
    raw = subprocess.run(["ffmpeg", "-v", "error", "-t", str(max_sec), "-i", path, "-ac", "1",
                          "-ar", str(SR), "-f", "f32le", "-"], capture_output=True).stdout
    x = np.frombuffer(raw, np.float32)
    if len(x) < SR * 5:
        return None
    nfr = 1 + (len(x) - N) // HOP
    idx = np.arange(N)[None, :] + HOP * np.arange(nfr)[:, None]
    mag = np.abs(np.fft.rfft(x[idx] * np.hanning(N).astype(np.float32), axis=1))
    logm = np.log1p(10 * mag)
    flux = np.maximum(0, np.diff(logm, axis=0)).sum(axis=1)
    k = int(SR / HOP * 0.5)
    flux = np.maximum(0, flux - np.convolve(flux, np.ones(k) / k, "same"))
    return flux / (flux.max() + 1e-9)


def beats(path):
    """{"bpm", "period", "first"} o None si no hay pulso claro."""
    env = onset_envelope(path)
    if env is None:
        return None
    fe = SR / HOP
    f = np.fft.rfft(env - env.mean(), 2 * len(env))
    ac = np.fft.irfft(f * np.conj(f))[:len(env)]
    best, bpm = -1.0, 120.0
    for b in np.arange(70, 180.5, 0.5):
        lag = 60 * fe / b
        lo = int(lag)
        val = ac[lo] + (ac[lo + 1] - ac[lo]) * (lag - lo)
        val *= np.exp(-0.5 * (np.log2(b / 120)) ** 2)
        if val > best:
            best, bpm = val, b
    # refinar: el BPM que mejor alinea una rejilla larga con los ataques (envolvente
    # suavizada e interpolada para que el puntaje varie suave con el BPM)
    g = np.exp(-0.5 * (np.arange(-4, 5) / 1.5) ** 2)
    envs = np.convolve(env, g / g.sum(), "same")
    xs = np.arange(len(envs))

    def grid(b):
        step = 60 / b * fe
        nb = int((len(envs) - 1) / step)
        if nb < 4:
            return -1.0, 0.0
        ks = step * np.arange(nb)
        sums = np.array([np.interp(o + ks, xs, envs).sum() for o in np.arange(0, step, 0.5)])
        i = int(np.argmax(sums))
        return float(sums[i]), i * 0.5
    cands = [(grid(b), b) for b in np.arange(bpm * 0.95, bpm * 1.05, 0.02)]
    (score, off), bpm = max(cands, key=lambda c: c[0][0])
    if score <= 0:
        return None
    period = 60 / bpm
    first = float((off / fe + LAT) % period)
    return {"bpm": round(float(bpm), 1), "period": float(period), "first": first}


def snap_durations(durs, period, transition, available):
    """Ajusta las duraciones para que cada transicion caiga en un beat.
    Clip 1: d = n*unit + T/2. Siguientes: d = n*unit + T. Nunca excede `available`."""
    unit = period * 2 if period < 0.75 else period
    out = []
    for k, (d, av) in enumerate(zip(durs, available)):
        extra = transition / 2 if k == 0 else transition
        n = max(1, int(round((d - extra) / unit)))
        while n > 1 and n * unit + extra > av + 1e-6:
            n -= 1
        out.append(min(av, n * unit + extra))
    return out, unit
