"""video_studio.quality - metricas tecnicas de calidad de video y mejor tramo por clip.

Todo se calcula con FFmpeg + NumPy (sin OpenCV), a baja resolucion:
- nitidez:     varianza del Laplaciano (escala logaritmica)
- exposicion:  penaliza tanto lo oscuro como lo quemado, y los pixeles recortados
- contraste:   desviacion estandar de la luminancia
- estabilidad: temblor real = cambios bruscos del desplazamiento entre fotogramas
               CONSECUTIVOS (correlacion de fase). Un paneo suave no se penaliza.
Ademas busca la ventana de N segundos con mejor puntaje dentro de cada clip.
"""
import argparse
import json
import os
import subprocess
import sys

import numpy as np

AW, AH = 640, 360            # resolucion de analisis
MW, MH = 160, 90             # resolucion para movimiento
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".mkv", ".avi", ".wmv", ".webm", ".mts", ".m2ts"}
SHARP_LO, SHARP_HI = 15.0, 900.0
WEIGHTS = {"sharpness": 0.35, "exposure": 0.25, "contrast": 0.15, "stability": 0.25}
LABELS = [(80, "EXCELENTE"), (60, "BUENO"), (40, "ACEPTABLE"), (0, "DESCARTAR")]


def label(score):
    return next(name for th, name in LABELS if score >= th)


def probe(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format",
                        "-show_streams", path], capture_output=True, text=True, errors="replace")
    if r.returncode != 0:
        raise RuntimeError(f"No pude leer {path}: {r.stderr[-300:]}")
    d = json.loads(r.stdout)
    v = next((s for s in d["streams"] if s["codec_type"] == "video"), None)
    if not v:
        raise RuntimeError(f"{path} no tiene video")
    return {"duration": float(d["format"].get("duration") or 0), "width": v.get("width"),
            "height": v.get("height"),
            "has_audio": any(s["codec_type"] == "audio" for s in d["streams"])}


def gray_frames(path, fps, hwaccel=True):
    """Genera fotogramas en gris (AH x AW, uint8) a `fps` por segundo."""
    cmd = ["ffmpeg", "-v", "error"]
    if hwaccel:
        cmd += ["-hwaccel", "auto"]
    cmd += ["-i", path, "-vf", f"fps={fps},scale={AW}:{AH},format=gray", "-an",
            "-f", "rawvideo", "-pix_fmt", "gray", "-"]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    size = AW * AH
    try:
        while True:
            buf = p.stdout.read(size)
            if len(buf) < size:
                break
            yield np.frombuffer(buf, np.uint8).reshape(AH, AW)
    finally:
        p.stdout.close()
        p.wait()


def sharpness(f):
    """Nitidez de la ZONA mas enfocada: con poca profundidad de campo (fondo
    desenfocado a proposito) el promedio del cuadro engana; se usan los mejores
    bloques de una grilla 8x6. Se normaliza el contraste (con limite) para que un
    perfil plano/log sin gradear no parezca borroso."""
    x = f.astype(np.float32)
    x = (x - x.mean()) * min(50.0 / (x.std() + 1e-6), 4.0)
    lap = (-4 * x[1:-1, 1:-1] + x[:-2, 1:-1] + x[2:, 1:-1] + x[1:-1, :-2] + x[1:-1, 2:])
    h, w = lap.shape
    th, tw = h // 6, w // 8
    tiles = lap[:th * 6, :tw * 8].reshape(6, th, 8, tw).var(axis=(1, 3)).ravel()
    var = float(np.sort(tiles)[-4:].mean())
    lo, hi = np.log10(SHARP_LO), np.log10(SHARP_HI)
    return var, float(np.clip((np.log10(var + 1e-6) - lo) / (hi - lo), 0, 1) * 100)


def exposure(f):
    m = float(f.mean())
    clipped = float(((f >= 250) | (f <= 5)).mean())
    base = 100 * (1 - min(1.0, abs(m - 118) / 110))
    return m, clipped, max(0.0, base - clipped * 200)


def contrast(f):
    s = float(f.std())
    return s, min(100.0, s / 55 * 100)


_HANN = None


def _subpix(c, i, axis):
    n = c.shape[axis]
    idx = [slice(None)] * 2
    get = lambda k: c[(k % n, i[1]) if axis == 0 else (i[0], k % n)]
    l, m, r = get(i[axis] - 1), get(i[axis]), get(i[axis] + 1)
    d = l - 2 * m + r
    return 0.0 if abs(d) < 1e-12 else 0.5 * (l - r) / d


def shift(a, b):
    """Desplazamiento (dy, dx) entre dos fotogramas por correlacion de fase, con
    precision sub-pixel. Devuelve tambien la confianza (altura del pico)."""
    global _HANN
    if _HANN is None:
        _HANN = np.outer(np.hanning(MH), np.hanning(MW)).astype(np.float32)
    a = (a - a.mean()) / (a.std() + 1e-6)
    b = (b - b.mean()) / (b.std() + 1e-6)
    fa = np.fft.fft2(a * _HANN)
    fb = np.fft.fft2(b * _HANN)
    r = fa * np.conj(fb)
    r /= np.abs(r) + 1e-9
    c = np.abs(np.fft.ifft2(r))
    i = np.unravel_index(np.argmax(c), c.shape)
    dy = i[0] + _subpix(c, i, 0)
    dx = i[1] + _subpix(c, i, 1)
    dy = dy - MH if dy > MH / 2 else dy
    dx = dx - MW if dx > MW / 2 else dx
    return float(dy), float(dx), float(c[i])


def small(f):
    # 640x360 -> 160x90 promediando bloques 4x4
    return f.reshape(MH, 4, MW, 4).mean(axis=(1, 3)).astype(np.float32)


def measure(path, fps=10, window=8.0, weights=None, hwaccel=True):
    """Mide un clip. Devuelve metricas globales, puntaje y el mejor tramo de `window` s."""
    weights = weights or WEIGHTS
    info = probe(path)
    rows, prev_s, prev_shift = [], None, None
    for i, f in enumerate(gray_frames(path, fps, hwaccel)):
        sv, ss = sharpness(f)
        mean, clip_frac, es = exposure(f)
        sd, cs = contrast(f)
        s = small(f)
        jerk, cut = None, False
        if prev_s is not None:
            diff = float(np.abs(s - prev_s).mean())
            cut = diff > 45  # corte de escena: no cuenta como temblor
            dy, dx, conf = shift(prev_s, s)
            unreliable = conf < 0.08 or s.std() < 6  # poca textura u oscuro: no se mide
            if unreliable:
                cut = True
            if prev_shift is not None and not cut:
                jerk = ((dy - prev_shift[0]) ** 2 + (dx - prev_shift[1]) ** 2) ** 0.5
                if jerk > 12:  # salto imposible para temblor: error de medicion
                    jerk = None
            prev_shift = None if cut else (dy, dx)
        prev_s = s
        rows.append({"t": i / fps, "sharp_raw": sv, "sharp": ss, "luma": mean,
                     "clipped": clip_frac, "exp": es, "std": sd, "con": cs, "jerk": jerk})
    if not rows and hwaccel:
        return measure(path, fps, window, weights, hwaccel=False)
    if not rows:
        raise RuntimeError(f"No pude decodificar {path}")

    jerks = np.array([r["jerk"] if r["jerk"] is not None else np.nan for r in rows])
    # temblor suavizado en ~1 s para el puntaje por fotograma
    k = max(1, int(fps))
    valid = np.nan_to_num(jerks, nan=0.0)
    cnt = np.convolve(~np.isnan(jerks), np.ones(k), "same")
    smooth = np.convolve(valid, np.ones(k), "same") / np.maximum(cnt, 1)
    stab = 100 * (1 - np.clip(smooth / 4.0, 0, 1))
    per = (weights["sharpness"] * np.array([r["sharp"] for r in rows])
           + weights["exposure"] * np.array([r["exp"] for r in rows])
           + weights["contrast"] * np.array([r["con"] for r in rows])
           + weights["stability"] * stab)

    n_win = max(1, int(round(window * fps)))
    if len(per) <= n_win:
        bs, be, bscore = 0.0, info["duration"], float(per.mean())
    else:
        cs_ = np.convolve(per, np.ones(n_win) / n_win, "valid")
        j = int(np.argmax(cs_))
        bs, be, bscore = j / fps, min(info["duration"], (j + n_win) / fps), float(cs_[j])

    def avg(key):
        return float(np.mean([r[key] for r in rows]))

    # mediana: el temblor real es constante; objetos moviendose dan picos aislados
    shake = float(np.nanmedian(jerks)) if np.any(~np.isnan(jerks)) else 0.0
    m = {"sharpness": round(avg("sharp"), 1), "exposure": round(avg("exp"), 1),
         "contrast": round(avg("con"), 1),
         "stability": round(100 * (1 - min(1.0, shake / 4.0)), 1)}
    score = sum(weights[k_] * m[k_] for k_ in weights)
    # Defectos que hacen inutil un clip aunque lo demas este bien
    if m["sharpness"] < 20 or m["exposure"] < 15:
        score = min(score, 39.0)
    elif m["stability"] < 25:
        score = min(score, 50.0)
    issues = []
    if m["sharpness"] < 40:
        issues.append("borroso/desenfocado")
    if avg("luma") < 50:
        issues.append("oscuro")
    elif avg("luma") > 200:
        issues.append("sobreexpuesto")
    if avg("clipped") > 0.08:
        issues.append(f"{avg('clipped') * 100:.0f}% de pixeles quemados/negros")
    if m["stability"] < 50:
        issues.append("tembloroso")
    if m["contrast"] < 35:
        issues.append("plano/sin contraste")
    return {"file": os.path.basename(path), "path": os.path.abspath(path),
            "duration": round(info["duration"], 2),
            "resolution": f"{info['width']}x{info['height']}", "has_audio": info["has_audio"],
            "score": round(score, 1), "label": label(score), "metrics": m, "issues": issues,
            "best_segment": {"start": round(bs, 2), "end": round(be, 2),
                             "score": round(bscore, 1)},
            "raw": {"laplacian_var": round(avg("sharp_raw"), 1), "luma": round(avg("luma"), 1),
                    "luma_std": round(avg("std"), 1), "shake_px": round(shake, 2)}}


def list_videos(paths):
    out = []
    for p in paths:
        if os.path.isdir(p):
            out += sorted(os.path.join(p, f) for f in os.listdir(p)
                          if os.path.splitext(f)[1].lower() in VIDEO_EXT)
        elif os.path.isfile(p):
            out.append(p)
        else:
            raise RuntimeError(f"No existe: {p}")
    return out


def to_markdown(results):
    lines = ["| # | Clip | Puntaje | Nivel | Nitidez | Exposicion | Contraste | Estabilidad "
             "| Mejor tramo | Problemas |", "|---|---|---|---|---|---|---|---|---|---|"]
    for i, r in enumerate(results, 1):
        m, b = r["metrics"], r["best_segment"]
        lines.append(f"| {i} | {r['file']} | {r['score']} | {r['label']} | {m['sharpness']} | "
                     f"{m['exposure']} | {m['contrast']} | {m['stability']} | "
                     f"{b['start']}-{b['end']}s | {', '.join(r['issues']) or '-'} |")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="vstudio quality",
                                 description="Puntaje tecnico de calidad (0-100) por clip")
    ap.add_argument("inputs", nargs="+", help="videos o carpetas")
    ap.add_argument("--window", type=float, default=8.0, help="duracion del mejor tramo (s)")
    ap.add_argument("--fps", type=float, default=10.0, help="fotogramas por segundo analizados")
    ap.add_argument("--no-hwaccel", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--report", help="guardar tabla markdown en este archivo")
    a = ap.parse_args(argv)
    try:
        vids = list_videos(a.inputs)
        if not vids:
            raise RuntimeError("No encontre videos.")
        res = []
        for v in vids:
            print(f"Analizando {os.path.basename(v)}...", file=sys.stderr, flush=True)
            try:
                res.append(measure(v, a.fps, a.window, hwaccel=not a.no_hwaccel))
            except RuntimeError as e:
                res.append({"file": os.path.basename(v), "path": v, "error": str(e),
                            "score": 0, "label": "ERROR"})
        res.sort(key=lambda r: r["score"], reverse=True)
    except RuntimeError as e:
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        sys.exit(1)
    ok = [r for r in res if "error" not in r]
    if a.report:
        with open(a.report, "w", encoding="utf-8") as fh:
            fh.write("# Reporte de calidad\n\n" + to_markdown(ok) + "\n")
    if a.json:
        print(json.dumps({"ok": True, "clips": res}, ensure_ascii=False, indent=2))
    else:
        print(to_markdown(ok))
        for r in res:
            if "error" in r:
                print(f"ERROR {r['file']}: {r['error']}")


if __name__ == "__main__":
    main()
