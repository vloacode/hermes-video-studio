"""video_studio.reframe - reencuadre inteligente de horizontal a vertical (o cuadrado).

Al pasar de 16:9 a 9:16 el recorte usa toda la altura y ~32 % del ancho: solo hay que
decidir su posicion horizontal (x) en cada instante.

1. Marcas del sujeto cada `--interval` s:
   - vlm: el modelo de vision ve el fotograma con columnas numeradas y dice en cual esta
     el sujeto pedido ("la cascada", "la orilla de la playa", "el rio"...).
   - saliency: sin modelo, la zona visualmente mas llamativa (residuo espectral).
2. Entre marcas se sigue el movimiento de la camara (correlacion de fase) para que el
   recorte compense giros y paneos del dron.
3. Suavizado tipo camarografo con velocidad maxima de paneo.
4. Render con FFmpeg moviendo el recorte cuadro a cuadro (sendcmd), y el recorrido se
   guarda en <salida>.path.json (para Shotcut y para reusarlo).
"""
import argparse
import base64
import io
import json
import os
import re
import sys
import tempfile
import types

import numpy as np

from . import quality as Q
from .edit import VeditError, ff, probe

ASPECTS = {"9:16": (9, 16), "4:5": (4, 5), "1:1": (1, 1), "3:4": (3, 4)}
OUT_H = {"9:16": 1920, "4:5": 1350, "1:1": 1080, "3:4": 1440}


def log(msg):
    print(msg, file=sys.stderr, flush=True)


# ------------------------------------------------------------------ camera motion

def camera_motion(path, fps, start, dur):
    """dx por fotograma de analisis, normalizado al ancho (positivo = la escena se
    movio a la izquierda). Tambien devuelve los fotogramas pequenos para saliencia."""
    cmd_frames = Q.gray_frames(path, fps, hwaccel=True, start=start, dur=dur)
    dxs, smalls, prev = [], [], None
    for f in cmd_frames:
        s = Q.small(f)
        if prev is None:
            dxs.append(0.0)
        else:
            dy, dx, conf = Q.shift(prev, s)
            diff = float(np.abs(s - prev).mean())
            ok = conf >= 0.06 and diff < 45 and abs(dx) < Q.MW * 0.25
            dxs.append(dx / Q.MW if ok else 0.0)
        smalls.append(s)
        prev = s
    return np.array(dxs), smalls


# ------------------------------------------------------------------ subject detection

def saliency_x(small, crop_frac):
    """Centro x de la ventana (del ancho del recorte) con mas saliencia."""
    img = small.astype(np.float32)
    f = np.fft.fft2(img)
    logamp = np.log(np.abs(f) + 1e-6)
    phase = np.angle(f)
    k = np.ones((3, 3), np.float32) / 9
    pad = np.pad(logamp, 1, mode="edge")
    avg = sum(pad[i:i + logamp.shape[0], j:j + logamp.shape[1]] * k[i, j]
              for i in range(3) for j in range(3))
    sal = np.abs(np.fft.ifft2(np.exp((logamp - avg) + 1j * phase))) ** 2
    # contraste local tambien cuenta (una cascada blanca en un fondo oscuro)
    sal = sal / (sal.max() + 1e-9) + 0.5 * np.abs(img - img.mean()) / (img.std() * 4 + 1e-6)
    col = sal.sum(axis=0)
    w = max(1, int(round(len(col) * crop_frac)))
    win = np.convolve(col, np.ones(w), "valid")
    j = int(np.argmax(win))
    return (j + w / 2) / len(col), float(win[j] / (col.sum() + 1e-9))


def grid_image(path, t, cols, tmp, idx):
    from PIL import Image, ImageDraw, ImageFont
    out = os.path.join(tmp, f"k{idx:04d}.jpg")
    ff("-ss", f"{t:.3f}", "-i", os.path.abspath(path), "-frames:v", "1", "-vf",
       "scale=1024:-2", "-q:v", "3", out)
    im = Image.open(out).convert("RGB")
    d = ImageDraw.Draw(im)
    w, h = im.size
    try:
        font = ImageFont.truetype("arial.ttf", 28)
    except OSError:
        font = ImageFont.load_default()
    for c in range(cols):
        x0 = int(c * w / cols)
        d.line([(x0, 0), (x0, h)], fill=(255, 255, 0), width=2)
        d.rectangle([x0 + 2, 2, x0 + 36, 34], fill=(0, 0, 0))
        d.text((x0 + 6, 3), str(c + 1), fill=(255, 255, 0), font=font)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=85)
    return buf.getvalue()


def vlm_x(img_bytes, subject, cols, a):
    from . import analyze as A
    ns = types.SimpleNamespace(model=a.model, timeout=300)
    content = [
        {"type": "text", "text": (
            f"La imagen tiene {cols} columnas numeradas de 1 a {cols} (lineas amarillas). "
            f"Sujeto: {subject}. En que columna esta el CENTRO del sujeto? Si no se ve, "
            'responde col null. Responde SOLO JSON: {"col": <numero o null>, "conf": <0-1>}')},
        {"type": "image_url", "image_url": {
            "url": "data:image/jpeg;base64," + base64.b64encode(img_bytes).decode()}}]
    ans = A.chat(ns, content, max_tokens=60)
    m = re.search(r"\{.*?\}", ans, re.S)
    if not m:
        return None, 0.0
    try:
        j = json.loads(m.group(0))
    except ValueError:
        return None, 0.0
    c = j.get("col")
    if c is None:
        return None, 0.0
    c = min(max(int(c), 1), cols)
    return (c - 0.5) / cols, float(j.get("conf", 0.7) or 0.7)


# ------------------------------------------------------------------ path

def build_path(n, fps, keys, dxs, lo, hi):
    """Posicion x (centro del recorte, 0-1) por fotograma de analisis.
    keys: lista de (indice, x, peso). Entre marcas se propaga con el movimiento de camara
    hacia adelante y hacia atras y se mezcla linealmente."""
    if not keys:
        return np.full(n, 0.5)
    cum = np.cumsum(dxs)  # desplazamiento acumulado de la escena
    path = np.zeros(n)
    keys = sorted(keys)
    for i in range(n):
        prev = max((k for k in keys if k[0] <= i), default=None, key=lambda k: k[0])
        nxt = min((k for k in keys if k[0] >= i), default=None, key=lambda k: k[0])
        cand = []
        if prev:
            cand.append((prev[1] - (cum[i] - cum[prev[0]]), prev, i - prev[0]))
        if nxt:
            cand.append((nxt[1] + (cum[nxt[0]] - cum[i]), nxt, nxt[0] - i))
        if len(cand) == 2 and cand[0][1] is not cand[1][1]:
            d0, d1 = cand[0][2], cand[1][2]
            w0 = d1 / (d0 + d1) * cand[0][1][2]
            w1 = d0 / (d0 + d1) * cand[1][1][2]
            path[i] = (cand[0][0] * w0 + cand[1][0] * w1) / (w0 + w1 + 1e-9)
        else:
            path[i] = cand[0][0]
    return np.clip(path, lo, hi)


def smooth(path, fps, sigma_s, max_speed, lo, hi):
    if len(path) < 3:
        return path
    r = max(1, int(sigma_s * fps * 3))
    xs = np.arange(-r, r + 1)
    k = np.exp(-0.5 * (xs / (sigma_s * fps + 1e-9)) ** 2)
    k /= k.sum()
    p = np.convolve(np.pad(path, r, mode="edge"), k, "valid")
    # velocidad maxima de paneo (fraccion del ancho por segundo)
    vmax = max_speed / fps
    out = p.copy()
    for i in range(1, len(out)):
        out[i] = out[i - 1] + np.clip(out[i] - out[i - 1], -vmax, vmax)
    for i in range(len(out) - 2, -1, -1):  # pasada inversa para no ir atrasado
        out[i] = out[i + 1] + np.clip(out[i] - out[i + 1], -vmax, vmax)
    return np.clip((out + p) / 2, lo, hi)


def flight_path(dxs, fps, offset, lead, lo, hi):
    """Modo vuelo: respeta el movimiento del piloto. El recorte queda en `offset`
    (0.5 = centro) y solo se adelanta un poco hacia donde gira el dron, muy suave."""
    v = np.array(dxs) * fps                       # velocidad de la escena (anchos/s)
    r = max(1, int(fps * 4.5))                    # suavizado fuerte (~1.5 s sigma)
    xs = np.arange(-r, r + 1)
    k = np.exp(-0.5 * (xs / (fps * 1.5)) ** 2)
    k /= k.sum()
    v = np.convolve(np.pad(v, r, mode="edge"), k, "valid")
    # escena a la izquierda (v>0) = dron girando a la derecha -> adelantar el recorte
    return np.clip(offset + np.clip(v * lead, -0.12, 0.12), lo, hi)


# ------------------------------------------------------------------ main

def reframe(a):
    info = probe(a.input)
    W, H, dur_all = info["width"], info["height"], info["duration"]
    start = a.start or 0.0
    dur = (a.end - start) if a.end else dur_all - start
    rw, rh = ASPECTS[a.aspect]
    cw = int(round(H * rw / rh / 2) * 2)
    if cw >= W:
        raise VeditError("El video ya es igual o mas angosto que el formato pedido; usa "
                         "'edit reframe --mode blur/pad'.")
    frac = cw / W
    lo, hi = frac / 2, 1 - frac / 2
    fps = a.analysis_fps
    log(f"Movimiento de camara ({dur:.0f}s a {fps:g} fps)...")
    dxs, smalls = camera_motion(a.input, fps, start, dur)
    n = len(dxs)
    if n == 0:
        raise VeditError("No pude decodificar el video.")

    keys, marks = [], []
    step = max(1, int(round(a.interval * fps)))
    mode = a.method
    if mode == "auto":
        mode = "vlm" if a.subject else "flight"
    with tempfile.TemporaryDirectory() as tmp:
        for k, i in enumerate(range(0, n, step) if mode != "flight" else []):
            t = start + i / fps
            x, w, src = None, 0.0, None
            if mode == "vlm" and a.subject:
                try:
                    x, w = vlm_x(grid_image(a.input, t, a.columns, tmp, k), a.subject,
                                 a.columns, a)
                    src = "vlm"
                except Exception as e:  # router apagado, modelo sin vision, etc.
                    if a.method == "vlm":
                        raise VeditError(f"Fallo el modelo de vision: {e}")
                    log(f"Vision no disponible ({e}); uso saliencia.")
                    mode = "saliency"
            if x is None and mode != "center":
                x, s = saliency_x(smalls[i], frac)
                w, src = 0.35 + min(0.4, s * 4), "saliency"
            if x is None:
                x, w, src = 0.5, 0.3, "center"
            keys.append((i, float(np.clip(x, lo, hi)), max(w, 0.05)))
            marks.append({"t": round(t, 2), "x": round(float(x), 3), "conf": round(w, 2),
                          "source": src})
            if k % 10 == 0:
                log(f"  marca {k + 1}: t={t:.1f}s x={x:.2f} ({src})")

        if mode == "flight":
            path = flight_path(dxs, fps, a.offset, a.lead, lo, hi)
            path = smooth(path, fps, 0.8, a.max_speed if a.max_speed_set else 0.08, lo, hi)
            marks.append({"t": round(start, 2), "x": round(float(path[0]), 3),
                          "conf": 1.0, "source": "flight"})
        else:
            raw = build_path(n, fps, keys, dxs, lo, hi)
            path = smooth(raw, fps, a.smoothing, a.max_speed, lo, hi)

        # comandos de recorte (en pixeles de la fuente), interpolados a ~30 por segundo
        out_fps = 30.0
        ts = np.arange(0, dur, 1 / out_fps)
        xs = np.interp(ts, np.arange(n) / fps, path)
        px = np.clip(np.round(xs * W - cw / 2), 0, W - cw).astype(int)
        cmds = os.path.join(tmp, "cmds.txt")
        with open(cmds, "w") as fh:
            for t, x in zip(ts, px):
                fh.write(f"{t:.3f} crop x {x};\n")
        oh = min(OUT_H[a.aspect], H)
        ow = int(round(oh * rw / rh / 2) * 2)
        o = os.path.abspath(a.output or os.path.splitext(a.input)[0] + f"_{a.aspect.replace(':', 'x')}_smart.mp4")
        args = ["-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", os.path.abspath(a.input),
                "-vf", f"sendcmd=f=cmds.txt,crop=w={cw}:h={H}:x={px[0]}:y=0,"
                       f"scale={ow}:{oh}:flags=lanczos,setsar=1,format=yuv420p",
                "-c:v", "libx264", "-preset", "medium", "-crf", "18",
                "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", o]
        log("Renderizando recorte...")
        ff(*args, cwd=tmp)

    path_file = os.path.splitext(o)[0] + ".path.json"
    with open(path_file, "w", encoding="utf-8") as fh:
        json.dump({"source": os.path.abspath(a.input), "start": start, "duration": dur,
                   "src_width": W, "src_height": H, "crop_width": cw, "aspect": a.aspect,
                   "fps": fps, "subject": a.subject, "x_center": [round(float(v), 4) for v in path],
                   "marks": marks}, fh, ensure_ascii=False)
    res = probe(o)
    srcs = {m["source"] for m in marks}
    return {"ok": True, "output": o, "path_file": path_file, "duration": res["duration"],
            "width": res["width"], "height": res["height"], "marks": len(marks),
            "method": "+".join(sorted(srcs)), "subject": a.subject,
            "pan_range": [round(float(path.min()), 3), round(float(path.max()), 3)]}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="vstudio reframe",
                                 description="Horizontal -> vertical siguiendo al sujeto")
    ap.add_argument("input")
    ap.add_argument("-o", "--output")
    ap.add_argument("--aspect", choices=list(ASPECTS), default="9:16")
    ap.add_argument("--subject", default="", help='que seguir: "la cascada", "el rio"...')
    ap.add_argument("--method", choices=["auto", "flight", "vlm", "saliency", "center"],
                    default="auto",
                    help="auto = 'vlm' si das --subject, si no 'flight' (sigue el vuelo: "
                         "centrado y suave, ideal para dron)")
    ap.add_argument("--offset", type=float, default=0.5,
                    help="flight: posicion base del recorte (0 izquierda, 0.5 centro, 1 derecha)")
    ap.add_argument("--lead", type=float, default=0.25,
                    help="flight: cuanto se adelanta hacia donde gira el dron (0 = fijo)")
    ap.add_argument("--model", default=os.environ.get("VIDEO_ANALYZER_MODEL", "gemma4-26b"))
    ap.add_argument("--interval", type=float, default=1.0, help="segundos entre marcas")
    ap.add_argument("--columns", type=int, default=12)
    ap.add_argument("--analysis-fps", type=float, default=10.0)
    ap.add_argument("--smoothing", type=float, default=1.2, help="suavizado (s)")
    ap.add_argument("--max-speed", type=float, default=0.2,
                    help="paneo maximo (fraccion del ancho por segundo)")
    ap.add_argument("--start", type=float, default=0.0)
    ap.add_argument("--end", type=float)
    a = ap.parse_args(argv)
    a.max_speed_set = argv is not None and "--max-speed" in argv or "--max-speed" in sys.argv
    try:
        res = reframe(a)
    except (VeditError, RuntimeError) as e:
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False, indent=2))
        sys.exit(1)
    print(json.dumps(res, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
