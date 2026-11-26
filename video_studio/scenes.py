"""video_studio.scenes - firmas visuales y analisis de tramos para elegir bien que cortar.

- Firma por instante: color (histograma YUV de 64 bins) + estructura (luminancia 16x9
  normalizada). Sirve para saber si dos tramos muestran lo mismo aunque vengan de clips
  distintos (mismo camino grabado dos veces, version estabilizada, etc.).
- Tramos: ventanas candidatas dentro de cada clip, puntuadas por calidad y por como se
  mueve la camara (sin cambios de direccion, entrando y saliendo en movimiento estable).
- Seleccion: voraz con diversidad (MMR). Cada tramo nuevo debe ser distinto de todos los
  ya elegidos; asi un clip largo puede aportar 2 escenas y dos clips iguales aportan 1.
"""
import numpy as np


def frame_sig(y, u, v):
    """Firma de un fotograma YUV420 (y: AHxAW, u/v: AH/2 x AW/2)."""
    h, w = y.shape
    ys = y.reshape(h // 2, 2, w // 2, 2).mean(axis=(1, 3))
    q = (np.stack([ys, u, v], -1) // 64).astype(np.int64).reshape(-1, 3)
    hist = np.bincount(q[:, 0] * 16 + q[:, 1] * 4 + q[:, 2], minlength=64).astype(np.float32)
    hist /= hist.sum() + 1e-9
    g = y.reshape(9, h // 9, 16, w // 16).mean(axis=(1, 3)).astype(np.float32)
    g = (g - g.mean()) / (g.std() + 1e-6)
    return hist, g


def distance(ca, sa, cb, sb):
    """0 = identico, ~0.15 = misma escena, >0.3 = escenas distintas.
    ca/cb: histogramas promedio (64,), sa/sb: estructuras (n, 9, 16)."""
    color = float(np.sqrt(max(0.0, 1.0 - np.sum(np.sqrt(ca * cb)))))
    struct = float(np.abs(sa[:, None] - sb[None]).mean(axis=(2, 3)).min())
    return 0.5 * color + 0.5 * struct


def _smooth(x, k):
    k = max(1, min(int(k), len(x)))
    return np.convolve(x, np.ones(k) / k, "same")


def motion_profile(tr, fps):
    """Velocidad suavizada, aceleracion y cambios de direccion de la camara."""
    dx, dy = tr["dx"].astype(float), tr["dy"].astype(float)
    k = max(1, int(round(fps * 0.4)))
    vx, vy = _smooth(dx, k), _smooth(dy, k)
    speed = np.hypot(vx, vy)
    acc = np.hypot(np.diff(vx, prepend=vx[0]), np.diff(vy, prepend=vy[0])) * fps
    acc = _smooth(acc, k)
    return vx, vy, speed, acc


def reversals(v, lo, hi):
    """True si la camara cambia de sentido con fuerza dentro de [lo, hi): ambos sentidos
    superan el 35 % del movimiento maximo del tramo (y un minimo absoluto)."""
    seg = v[lo:hi]
    if not len(seg):
        return False
    thr = max(0.3, 0.35 * float(np.abs(seg).max()))
    return bool(seg.max() > thr and seg.min() < -thr)


FAST = 5.0   # px por fotograma a 160 px de ancho (10 fps): paneo/giro brusco


def windows(res, tr, fps, length, min_len, step=0.5):
    """Tramos candidatos de un clip (duracion variable entre min_len y length) con su
    puntaje y los motivos de penalizacion."""
    per = tr["per"]
    n = len(per)
    if not n:
        return []
    cut = tr["cut"].astype(bool)
    vx, vy, speed, acc = motion_profile(tr, fps)
    acc_ref = max(0.5, float(np.percentile(acc, 90)))
    half = max(1, int(fps * 0.5))
    lens = sorted({int(round(min(L, n / fps) * fps))
                   for L in np.arange(length, min_len - 1e-6, -1.0)}, reverse=True)
    lens = [L for L in lens if L >= int(min_len * fps)]
    # fraccion de pixeles muy oscuros / muy claros por firma (bins de Y del histograma)
    st = tr["sig_t"]
    dark = tr["sig_color"][:, :16].sum(axis=1)
    bright = tr["sig_color"][:, 48:].sum(axis=1)
    out = []
    for L in lens:
        for s in range(0, n - L + 1, max(1, int(step * fps))):
            e = s + L
            m = (st >= s / fps) & (st <= e / fps)
            if m.any() and (dark[m].mean() > 0.55 or bright[m].mean() > 0.6):
                continue  # tramo casi negro (interior, sombra) o quemado
            q = float(per[s:e].mean())
            a_in = float(acc[s:s + half].mean()) / acc_ref
            a_out = float(acc[e - half:e].mean()) / acc_ref
            inner = float(acc[s:e].mean()) / acc_ref
            rev = reversals(vx, s, e) or reversals(vy, s, e)
            fast = float(speed[s:e].max()) > FAST
            cuts = int(cut[s + 1:e].sum())
            pen = (12 * inner + 10 * (a_in + a_out) + (25 if rev else 0)
                   + (15 if fast else 0) + (30 if cuts else 0))
            why = []
            if rev:
                why.append("cambio de direccion")
            if fast:
                why.append("paneo/giro brusco")
            if cuts:
                why.append("salto de imagen")
            if a_in > 0.8 or a_out > 0.8:
                why.append("entra/sale a mitad de maniobra")
            out.append({"start": s / fps, "end": e / fps, "quality": q,
                        "score": q - pen + 1.5 * L / fps, "issues": why})
    return out


def window_sig(tr, start, end):
    t = tr["sig_t"]
    idx = np.where((t >= start) & (t <= end))[0]
    if not len(idx):
        idx = np.array([int(np.argmin(np.abs(t - (start + end) / 2)))])
    sel = idx[np.linspace(0, len(idx) - 1, min(4, len(idx))).round().astype(int)]
    return tr["sig_color"][idx].mean(axis=0), tr["sig_struct"][sel]


# ------------------------------------------------------------------ escenas

def segments(tr, thr=0.3, min_len=2.0):
    """Divide un clip en escenas: empieza una nueva cuando la imagen se aleja de forma
    sostenida (1 s) de como empezo la escena actual. Devuelve [(ini, fin), ...] en s."""
    t, C, St = tr["sig_t"], tr["sig_color"], tr["sig_struct"]
    n = len(t)
    if not n:
        return []
    bounds, a, far = [0], 0, 0
    for i in range(1, n):
        d = distance(C[a:a + 2].mean(0), St[a:a + 2], C[i], St[i:i + 1])
        far = far + 1 if d > thr else 0
        if far >= 2:
            a = i - 1
            bounds.append(a)
            far = 0
    end = float(t[-1]) + float(t[1] - t[0] if n > 1 else 0.5)
    segs = [(float(t[b]), float(t[bounds[k + 1]]) if k + 1 < len(bounds) else end)
            for k, b in enumerate(bounds)]
    merged = []
    for s, e in segs:
        if merged and (e - s < min_len):
            merged[-1] = (merged[-1][0], e)
        else:
            merged.append((s, e))
    if len(merged) > 1 and merged[0][1] - merged[0][0] < min_len:
        merged[1] = (merged[0][0], merged[1][1])
        merged.pop(0)
    return merged


def span_sig(tr, start, end, k=8):
    t = tr["sig_t"]
    idx = np.where((t >= start) & (t < end))[0]
    if not len(idx):
        idx = np.array([int(np.argmin(np.abs(t - (start + end) / 2)))])
    sel = idx[np.linspace(0, len(idx) - 1, min(k, len(idx))).round().astype(int)]
    return tr["sig_color"][idx].mean(axis=0), tr["sig_struct"][sel]


def cluster(sigs, thr):
    """Agrupa firmas (enlace promedio). Devuelve una etiqueta entera por firma."""
    n = len(sigs)
    D = np.zeros((n, n))
    for i in range(n):
        for j in range(i + 1, n):
            D[i, j] = D[j, i] = distance(sigs[i][0], sigs[i][1], sigs[j][0], sigs[j][1])
    groups = [[i] for i in range(n)]
    while len(groups) > 1:
        best, bi, bj = None, -1, -1
        for i in range(len(groups)):
            for j in range(i + 1, len(groups)):
                d = D[np.ix_(groups[i], groups[j])].mean()
                if best is None or d < best:
                    best, bi, bj = d, i, j
        if best > thr:
            break
        groups[bi] += groups.pop(bj)
    labels = np.zeros(n, int)
    for g, members in enumerate(groups):
        labels[members] = g
    return labels
