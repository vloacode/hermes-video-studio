"""video_studio.autoedit - de una carpeta de clips a un video final, automaticamente.

1. Mide la calidad tecnica de cada clip y encuentra su mejor tramo (quality.py).
2. (Opcional --vision) un modelo con vision puntua que tan atractiva es cada toma.
3. Quita tomas casi iguales y elige las mejores hasta la duracion objetivo.
4. Recorta el mejor tramo de cada clip, adapta el formato (9:16, 16:9...), une con
   transiciones, agrega titulo y musica (con ducking) y normaliza el volumen.
5. Deja reporte (Markdown), timeline JSON y EDL (DaVinci/Premiere) junto al video.
"""
import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
import types
from datetime import datetime

from . import quality as Q
from .edit import (ENC, PRESETS, VeditError, ff, find_font, probe, reframe_filter,
                   stage_assets)

ORDER_LABELS = {"EXCELENTE": 3, "BUENO": 2, "ACEPTABLE": 1, "DESCARTAR": 0}


def log(msg):
    print(msg, file=sys.stderr, flush=True)


# ------------------------------------------------------------------ analysis (with cache)

CACHE_VER = "v2"


def file_key(p):
    st = os.stat(p)
    return f"{os.path.abspath(p)}|{st.st_size}|{int(st.st_mtime)}"


def analyze_clips(videos, outdir, window, fps):
    """Calidad + trazo (movimiento de camara y firmas visuales) de cada clip, con cache."""
    import numpy as np
    cache_path = os.path.join(outdir, "quality.json")
    tdir = os.path.join(outdir, ".trace")
    os.makedirs(tdir, exist_ok=True)
    cache = {}
    if os.path.exists(cache_path):
        try:
            with open(cache_path, encoding="utf-8") as fh:
                cache = {c["_key"]: c for c in json.load(fh) if "_key" in c}
        except (ValueError, KeyError):
            cache = {}
    results, traces = [], {}
    for i, v in enumerate(videos, 1):
        key = f"{file_key(v)}|{window}|{fps}|{CACHE_VER}"
        tpath = os.path.join(tdir, hashlib.sha1(key.encode()).hexdigest()[:16] + ".npz")
        if key in cache and (os.path.exists(tpath) or "error" in cache[key]):
            results.append(cache[key])
            if os.path.exists(tpath):
                traces[key] = dict(np.load(tpath))
            continue
        log(f"[{i}/{len(videos)}] Calidad y movimiento: {os.path.basename(v)}")
        try:
            r = Q.measure(v, fps=fps, window=window, trace=True)
            traces[key] = r.pop("_trace")
            np.savez(tpath, **traces[key])
        except RuntimeError as e:
            r = {"file": os.path.basename(v), "path": os.path.abspath(v), "error": str(e),
                 "score": 0, "label": "ERROR"}
        r["_key"] = key
        results.append(r)
        with open(cache_path, "w", encoding="utf-8") as fh:  # guardado incremental
            json.dump(results, fh, ensure_ascii=False, indent=1)
    return results, traces


# ------------------------------------------------------------------ vision review

def middle_frame(path, t, out):
    ff("-ss", f"{t:.3f}", "-i", path, "-frames:v", "1", "-vf", "scale=480:-2", "-q:v", "3", out)
    return out


def vision_review(cands, a, tmp):
    """Una sola llamada al modelo: puntaje 1-10 de cada tramo y grupo (misma escena)."""
    from . import analyze as A
    import re
    frames = []
    for i, c in enumerate(cands, 1):
        t = (c["segment"][0] + c["segment"][1]) / 2
        p = middle_frame(c["path"], t, os.path.join(tmp, f"v{i:03d}.jpg"))
        frames.append({"path": p, "t": t, "n": i})
    ns = types.SimpleNamespace(model=a.model, timeout=600)
    content = [{"type": "text", "text": (
        f"Estas grillas muestran {len(frames)} tomas numeradas (#n) candidatas para un video"
        f"{' sobre: ' + a.topic if a.topic else ''}. Para cada toma da: "
        "\"p\" = puntaje 1-10 de que tan atractiva, clara e interesante es (composicion, "
        "sujeto, luz); \"g\" = una letra de grupo: las tomas que muestran el MISMO lugar o "
        "sujeto desde un angulo parecido llevan la MISMA letra (son repetidas). "
        "Responde SOLO un JSON como {\"1\": {\"p\": 7, \"g\": \"A\"}, "
        "\"2\": {\"p\": 5, \"g\": \"A\"}}.")}]
    content += [A.img_part(s) for s in A.contact_sheets(frames)]
    try:
        ans = A.chat(ns, content, max_tokens=1200)
        m = re.search(r"\{.*\}", ans, re.S)
        data = json.loads(m.group(0)) if m else {}
    except Exception as e:  # el modelo es opcional: si falla seguimos sin vision
        log(f"Vision no disponible ({e}); sigo solo con el analisis tecnico.")
        return False
    for i, c in enumerate(cands, 1):
        v = data.get(str(i))
        if isinstance(v, (int, float)):
            c["vision"] = max(1.0, min(10.0, float(v)))
        elif isinstance(v, dict):
            if isinstance(v.get("p"), (int, float)):
                c["vision"] = max(1.0, min(10.0, float(v["p"])))
            if v.get("g"):
                c["group"] = str(v["g"]).strip().upper()[:3]
    return True


# ------------------------------------------------------------------ selection

def candidates(results, traces, a):
    """Todos los tramos posibles de todos los clips que pasan el nivel minimo, cada uno
    con su escena: los clips se dividen en escenas y las escenas iguales entre clips
    (mismo camino grabado dos veces, version estabilizada...) comparten numero."""
    from . import scenes as S
    min_rank = ORDER_LABELS[a.min_label]
    floor = min(t for t, n in Q.LABELS if ORDER_LABELS.get(n, 0) >= min_rank)
    ok = [r for r in results if "error" not in r and ORDER_LABELS.get(r["label"], 0) >= min_rank]

    segs = []  # (path, ini, fin, firma)
    for r in ok:
        tr = traces.get(r.get("_key"))
        if tr is not None:
            for s0, e0 in S.segments(tr, a.scene_threshold):
                segs.append((r["path"], s0, e0, S.span_sig(tr, s0, e0)))
    labels = S.cluster([x[3] for x in segs], a.scene_threshold) if segs else []
    scene_of = {}
    for (path, s0, e0, _), lab in zip(segs, labels):
        scene_of.setdefault(path, []).append((s0, e0, int(lab)))

    out = []
    for r in ok:
        tr = traces.get(r.get("_key"))
        if tr is None:  # sin trazo: el mejor tramo de calidad, como antes
            b = r["best_segment"]
            out.append({**r, "segment": [b["start"], min(b["end"], b["start"] + a.clip_max)],
                        "seg_quality": b["score"], "seg_score": b["score"], "notes": [],
                        "sig": None, "scene": None})
            continue
        spans = scene_of.get(r["path"], [])
        for w in S.windows(r, tr, a.fps, a.clip_max, a.clip_min):
            if w["quality"] < floor:  # tramo oscuro/borroso dentro de un clip bueno
                continue
            mid = (w["start"] + w["end"]) / 2
            sc = next((lab for s0, e0, lab in spans if s0 <= mid < e0),
                      spans[-1][2] if spans else None)
            notes, score = list(w["issues"]), w["score"]
            if any(w["start"] + 0.5 < e0 < w["end"] - 0.5 for _, e0, _ in spans):
                notes.append("cruza un cambio de escena")
                score -= 8
            out.append({**r, "segment": [round(w["start"], 2), round(w["end"], 2)],
                        "seg_quality": round(w["quality"], 1), "seg_score": score,
                        "notes": notes, "sig": S.window_sig(tr, w["start"], w["end"]),
                        "scene": sc})
    return out


def pick(cands, a, need, key):
    """Voraz con diversidad: el mejor tramo que no se solape ni se parezca a los elegidos."""
    from . import scenes as S
    chosen, why_not, total = [], {}, 0.0
    per_clip, per_scene = {}, {}
    for c in sorted(cands, key=key, reverse=True):
        if total >= need:
            break
        s, e = c["segment"]
        if any(o["path"] == c["path"] and s < o["segment"][1] + 1 and e > o["segment"][0] - 1
               for o in chosen):
            continue
        if per_clip.get(c["path"], 0) >= a.per_clip:
            continue
        twin = None
        if c.get("scene") is not None and per_scene.get(c["scene"], 0) >= a.per_scene:
            o = next(o for o in chosen if o.get("scene") == c["scene"])
            twin = (o, "misma escena")
        for o in ([] if twin else chosen):
            if c.get("group") and c.get("group") == o.get("group"):
                twin = (o, "el modelo la ve como la misma escena")
                break
            if c["sig"] is not None and o["sig"] is not None:
                d = S.distance(c["sig"][0], c["sig"][1], o["sig"][0], o["sig"][1])
                if d < a.similar:
                    twin = (o, f"distancia {d:.2f}")
                    break
        if twin:
            o, how = twin
            why_not.setdefault(c["path"], f"parecida a {o['file']} "
                                          f"{o['segment'][0]}-{o['segment'][1]}s ({how})")
            continue
        chosen.append(c)
        per_clip[c["path"]] = per_clip.get(c["path"], 0) + 1
        if c.get("scene") is not None:
            per_scene[c["scene"]] = per_scene.get(c["scene"], 0) + 1
        total += (e - s) - (a.transition if len(chosen) > 1 else 0)
    return chosen, why_not


def select(results, traces, a, tmp):
    cands = candidates(results, traces, a)
    if not cands:
        return [], []
    for c in cands:
        c["final"] = round(c["seg_score"], 1)
    if a.vision:
        # el modelo revisa un grupo amplio (el doble de lo necesario) y luego se elige
        pool, _ = pick(cands, a, a.max_target * 2, key=lambda c: c["final"])
        if vision_review(pool, a, tmp):
            for c in pool:
                if "vision" in c:
                    c["final"] = round(c["seg_score"] * 0.6 + c["vision"] * 4, 1)
            cands = pool
    chosen, why_not = pick(cands, a, a.target, key=lambda c: c["final"])

    # recortar el ultimo si nos pasamos del maximo
    total = sum(c["segment"][1] - c["segment"][0] for c in chosen) \
        - a.transition * max(0, len(chosen) - 1)
    if chosen and total > a.max_target:
        last = chosen[-1]
        last["segment"][1] = round(max(last["segment"][0] + a.clip_min,
                                       last["segment"][1] - (total - a.max_target)), 2)

    used = {c["path"] for c in chosen}
    has_cand = {c["path"] for c in cands}
    dropped = []
    for r in results:
        if "error" in r or r["path"] in used:
            continue
        if ORDER_LABELS.get(r["label"], 0) < ORDER_LABELS[a.min_label]:
            reason = f"calidad {r['score']} ({r['label']})"
        else:
            reason = why_not.get(r["path"]) or (
                "no hizo falta para la duracion objetivo" if r["path"] in has_cand
                else "sin tramos utiles (muy corto, oscuro o con maniobras bruscas)")
        dropped.append({**r, "dropped": reason})

    if a.order == "name":
        chosen.sort(key=lambda c: (c["file"].lower(), c["segment"][0]))
    elif a.order == "time":
        chosen.sort(key=lambda c: (os.path.getmtime(c["path"]), c["segment"][0]))
    return chosen, dropped


def sync_to_music(chosen, a):
    """Ajusta las duraciones para que cada transicion caiga en un beat de la musica."""
    from . import music as M
    info = M.beats(a.music)
    if not info:
        log("No detecte un pulso claro en la musica; sin sincronizar cortes.")
        return None
    durs = [c["segment"][1] - c["segment"][0] for c in chosen]
    avail = [c["duration"] - c["segment"][0] for c in chosen]
    new, unit = M.snap_durations(durs, info["period"], a.transition, avail)
    for c, d in zip(chosen, new):
        c["segment"][1] = round(c["segment"][0] + d, 3)
    info["cut_every"] = round(unit, 3)
    log(f"Musica: {info['bpm']} BPM; cortes cada {unit:.2f}s x n")
    return info


# ------------------------------------------------------------------ render

def render(chosen, a, outdir, tmp):
    w, h, fps, br, _ = PRESETS[a.preset]
    segs = []
    for i, c in enumerate(chosen, 1):
        s, e = c["segment"]
        out = os.path.join(tmp, f"seg{i:03d}.mp4")
        log(f"Recortando {i}/{len(chosen)}: {c['file']} {s}-{e}s")
        vf = reframe_filter(w, h, a.mode) + f",fps={fps},format=yuv420p"
        args = ["-ss", f"{s:.3f}", "-t", f"{e - s:.3f}", "-i", c["path"]]
        if c.get("has_audio") and not a.mute_clips:
            amap = ["-map", "0:a:0", "-af",
                    f"aresample=48000,aformat=channel_layouts=stereo,volume={a.clip_volume}"]
        else:
            args += ["-f", "lavfi", "-t", f"{e - s:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
            amap = ["-map", "1:a:0"]
        ff(*args, "-filter_complex", f"[0:v]{vf}[v]", "-map", "[v]", *amap,
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "16", "-c:a", "aac",
           "-b:a", "192k", "-shortest", out)
        segs.append((out, probe(out)["duration"]))

    # unir con transiciones
    T = a.transition if len(segs) > 1 else 0
    inputs, vf, af = [], [], []
    for p, _ in segs:
        inputs += ["-i", p]
    if T > 0:
        acc = segs[0][1]
        vlast, alast = "0:v", "0:a"
        for k in range(1, len(segs)):
            off = acc - T
            vf.append(f"[{vlast}][{k}:v]xfade=transition={a.xfade}:duration={T}:"
                      f"offset={off:.3f}[vx{k}]")
            af.append(f"[{alast}][{k}:a]acrossfade=d={T}[ax{k}]")
            vlast, alast = f"vx{k}", f"ax{k}"
            acc += segs[k][1] - T
        chain = vf + af
        vout, aout = f"[{vlast}]", f"[{alast}]"
    else:
        chain = ["".join(f"[{k}:v][{k}:a]" for k in range(len(segs)))
                 + f"concat=n={len(segs)}:v=1:a=1[vc][ac]"]
        vout, aout = "[vc]", "[ac]"
    total = sum(d for _, d in segs) - T * (len(segs) - 1)

    # titulo
    if a.title:
        tf = os.path.join(tmp, "title.txt")
        with open(tf, "w", encoding="utf-8") as fh:
            fh.write(a.title)
        size = int(h * 0.055)
        assets = stage_assets(tmp, font=find_font(a.font))
        chain.append(
            f"{vout}drawtext=fontfile={assets['font']}:textfile=title.txt:"
            f"fontsize={size}:fontcolor=white:borderw=4:bordercolor=black@0.7:"
            f"x=(w-text_w)/2:y=h*0.12:enable='between(t,0.3,{a.title_seconds})':"
            f"alpha='if(lt(t,0.8),(t-0.3)/0.5,if(gt(t,{a.title_seconds - 0.5}),"
            f"({a.title_seconds}-t)/0.5,1))'[vt]")
        vout = "[vt]"
    # fundido final
    chain.append(f"{vout}fade=t=in:st=0:d=0.4,fade=t=out:st={max(total - 0.8, 0):.3f}:d=0.8[vf]")
    vout = "[vf]"

    # musica
    if a.music:
        mi = len(segs)
        inputs += ["-stream_loop", "-1", "-i", a.music]
        b0 = getattr(a, "_music_start", 0.0)
        mus = (f"[{mi}:a]atrim=start={b0:.3f},asetpts=PTS-STARTPTS,volume={a.music_volume},"
               f"atrim=0:{total:.3f},afade=t=in:d=1,"
               f"afade=t=out:st={max(total - 2, 0):.3f}:d=2[m]")
        if a.duck:
            chain += [mus, f"{aout}asplit[c1][c2]",
                      "[m][c1]sidechaincompress=threshold=0.04:ratio=8:attack=20:release=400[md]",
                      "[c2][md]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[am]"]
        else:
            chain += [mus, f"{aout}[m]amix=inputs=2:duration=first:dropout_transition=0:"
                      "normalize=0[am]"]
        aout = "[am]"
    chain.append(f"{aout}loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo[af]")
    aout = "[af]"

    name = a.name or os.path.basename(os.path.normpath(a.inputs[0])) or "video"
    final = os.path.join(outdir, f"{name}_{a.preset}.mp4")
    v = ["-c:v", "libx264", "-preset", "slow", "-crf", "19", "-maxrate", br, "-bufsize", br,
         "-pix_fmt", "yuv420p"] if ENC == "libx264" else ["-c:v", ENC, "-b:v", br,
                                                          "-pix_fmt", "yuv420p"]
    log("Renderizando video final...")
    final = os.path.abspath(final)
    inputs = [os.path.abspath(x) if os.path.exists(x) else x for x in inputs]
    ff(*inputs, "-filter_complex", ";".join(chain), "-map", vout, "-map", aout, *v,
       "-c:a", "aac", "-b:a", "192k", "-t", f"{total:.3f}", "-movflags", "+faststart", final,
       cwd=tmp)
    return final, segs, total


# ------------------------------------------------------------------ outputs

def tc(sec, fps=30):
    f = int(round(sec * fps))
    return f"{f // (3600 * fps):02d}:{f // (60 * fps) % 60:02d}:{f // fps % 60:02d}:{f % fps:02d}"


def write_edl(chosen, path, title, fps=30):
    rec = 3600.0
    lines = [f"TITLE: {title}", "FCM: NON-DROP FRAME", ""]
    for i, c in enumerate(chosen, 1):
        s, e = c["segment"]
        d = e - s
        lines.append(f"{i:03d}  AX       V     C        {tc(s, fps)} {tc(e, fps)} "
                     f"{tc(rec, fps)} {tc(rec + d, fps)}")
        lines.append(f"* FROM CLIP NAME: {c['file']}")
        lines.append("")
        rec += d
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))


def write_report(path, a, results, chosen, dropped, final, total):
    ok = [r for r in results if "error" not in r]
    L = [f"# {a.name or 'Video'} - reporte de autoedit", "",
         f"- Fecha: {datetime.now():%Y-%m-%d %H:%M}",
         f"- Clips analizados: {len(results)} | elegidos: {len(chosen)}",
         f"- Formato: {a.preset} ({PRESETS[a.preset][4]}) | duracion final: {total:.1f}s",
         f"- Video: `{final}`" if final else "- Video: (no renderizado, --dry-run)", "",
         "## Linea de tiempo", "",
         "| # | Clip | Tramo | Escena | Calidad del tramo | Vision | Puntaje | Notas |",
         "|---|---|---|---|---|---|---|---|"]
    for i, c in enumerate(chosen, 1):
        L.append(f"| {i} | {c['file']} | {c['segment'][0]}-{c['segment'][1]}s | "
                 f"{c.get('scene', '-')} | {c.get('seg_quality', c['score'])} | "
                 f"{c.get('vision', '-')} | {c['final']} | {', '.join(c.get('notes') or []) or '-'} |")
    L += ["", f"Cada tramo se elige sin cambios de direccion ni maniobras a medias, y como "
          f"maximo {a.per_scene} por escena (escenas iguales en clips distintos cuentan juntas)."]
    if total < a.target:
        L += ["", f"Nota: solo hay material distinto para {total:.0f}s (objetivo {a.target:.0f}s). "
              "Para alargarlo sube --per-scene o --clip-max."]
    L += ["", "## Calidad de todos los clips", "", Q.to_markdown(ok)]
    errs = [r for r in results if "error" in r]
    if errs or dropped:
        L += ["", "## No usados (y por que)", ""]
        L += [f"- {r['file']}: {r['error']}" for r in errs]
        L += [f"- {d['file']}: {d['dropped']}" for d in dropped]
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(L) + "\n")


# ------------------------------------------------------------------ main flow

def run_project(a, inputs, outdir):
    os.makedirs(outdir, exist_ok=True)
    videos = Q.list_videos(inputs)
    if not videos:
        raise VeditError("No encontre videos.")
    results, traces = analyze_clips(videos, outdir, a.clip_max, a.fps)
    beat = None
    with tempfile.TemporaryDirectory() as tmp:
        chosen, dropped = select(results, traces, a, tmp)
        if chosen and a.music and a.beat_sync:
            beat = sync_to_music(chosen, a)
            a._music_start = beat["first"] if beat else 0.0
        if not chosen:
            raise VeditError(f"Ningun clip alcanza el nivel minimo '{a.min_label}'. "
                             "Prueba --min-label DESCARTAR o revisa quality.json.")
        final, total = None, sum(c["segment"][1] - c["segment"][0] for c in chosen)
        if not a.dry_run:
            final, _, total = render(chosen, a, outdir, tmp)
    title = a.name or "autoedit"
    timeline = {"generated": datetime.now().isoformat(), "preset": a.preset,
                "transition": a.transition, "total_duration": round(total, 2),
                "output": final, "music_beat": beat,
                "clips": [{"file": c["file"], "path": c["path"], "in": c["segment"][0],
                           "out": c["segment"][1], "quality": c["score"],
                           "segment_quality": c.get("seg_quality"), "notes": c.get("notes", []),
                           "scene": c.get("scene"),
                           "vision": c.get("vision"), "group": c.get("group"),
                           "final": c["final"]} for c in chosen]}
    with open(os.path.join(outdir, "timeline.json"), "w", encoding="utf-8") as fh:
        json.dump(timeline, fh, ensure_ascii=False, indent=2)
    write_edl(chosen, os.path.join(outdir, "timeline.edl"), title)
    report = os.path.join(outdir, "reporte.md")
    write_report(report, a, results, chosen, dropped, final, total)
    res = {"ok": True, "output": final, "duration": round(total, 2), "clips_used": len(chosen),
           "clips_analyzed": len(results), "report": report,
           "timeline": os.path.join(outdir, "timeline.json"),
           "edl": os.path.join(outdir, "timeline.edl"),
           "clips": [f"{c['file']} {c['segment'][0]}-{c['segment'][1]}s" for c in chosen],
           "skipped": [f"{d['file']}: {d['dropped']}" for d in dropped]}
    if beat:
        res["music_bpm"] = beat["bpm"]
    if final:
        res.update({k: v for k, v in probe(final).items() if k in ("width", "height", "size_mb")})
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(prog="vstudio autoedit",
                                 description="Carpeta de clips -> video final automatico")
    ap.add_argument("inputs", nargs="*", help="carpeta(s) o videos")
    ap.add_argument("-o", "--outdir", help="carpeta de salida (default: <entrada>_autoedit)")
    ap.add_argument("--inbox", help="carpeta con un subproyecto por carpeta (procesa pendientes)")
    ap.add_argument("--outbox", help="destino de los proyectos de --inbox")
    ap.add_argument("--mark-done", action="store_true", help="renombra la carpeta a ' - DONE'")
    ap.add_argument("--name", help="nombre del proyecto/video")
    ap.add_argument("--preset", choices=list(PRESETS), default="reels")
    ap.add_argument("--mode", choices=["blur", "crop", "pad"], default="crop",
                    help="como adaptar al formato (crop llena la pantalla)")
    ap.add_argument("--target", type=float, default=60, help="duracion minima deseada (s)")
    ap.add_argument("--max-target", type=float, default=80, help="duracion maxima (s)")
    ap.add_argument("--clip-max", type=float, default=6, help="maximo por toma (s)")
    ap.add_argument("--clip-min", type=float, default=2, help="minimo por toma (s)")
    ap.add_argument("--min-label", choices=list(ORDER_LABELS), default="BUENO")
    ap.add_argument("--order", choices=["name", "time", "score"], default="name",
                    help="orden en el video (name/time = cronologico)")
    ap.add_argument("--similar", type=float, default=0.24,
                    help="0-1: tramos con distancia menor se consideran repetidos "
                         "(mas alto = descarta mas; 0 = no comparar)")
    ap.add_argument("--per-scene", type=int, default=1,
                    help="maximo de tramos por escena (escenas iguales entre clips cuentan juntas)")
    ap.add_argument("--scene-threshold", type=float, default=0.30,
                    help="0-1: que tan distinto debe verse algo para ser otra escena")
    ap.add_argument("--per-clip", type=int, default=2,
                    help="maximo de tramos de un mismo clip (si muestran escenas distintas)")
    ap.add_argument("--no-beat-sync", dest="beat_sync", action="store_false",
                    help="no ajustar los cortes al ritmo de la musica")
    ap.add_argument("--transition", type=float, default=0.35, help="segundos (0 = corte)")
    ap.add_argument("--xfade", default="fade", help="tipo: fade, fadeblack, wipeleft, smoothleft...")
    ap.add_argument("--title", help="titulo al inicio")
    ap.add_argument("--title-seconds", type=float, default=3.5)
    ap.add_argument("--font")
    ap.add_argument("--music", help="archivo de musica de fondo")
    ap.add_argument("--music-volume", type=float, default=0.25)
    ap.add_argument("--duck", action="store_true", help="baja la musica cuando hay voz")
    ap.add_argument("--mute-clips", action="store_true", help="quita el audio de los clips")
    ap.add_argument("--clip-volume", type=float, default=0.8)
    ap.add_argument("--vision", action="store_true", help="puntuar atractivo con modelo de vision")
    ap.add_argument("--topic", default="", help="tema del video, para --vision")
    ap.add_argument("--model", default=os.environ.get("VIDEO_ANALYZER_MODEL", "gemma4-26b"))
    ap.add_argument("--fps", type=float, default=10, help="fps de analisis de calidad")
    ap.add_argument("--dry-run", action="store_true", help="solo analiza y elige, no renderiza")
    a = ap.parse_args(argv)

    try:
        if a.music and not os.path.isfile(a.music):
            raise VeditError(f"No existe la musica: {a.music}")
        if a.inbox:
            out_root = a.outbox or os.path.join(a.inbox, "_autoedit")
            pending = [os.path.join(a.inbox, d) for d in sorted(os.listdir(a.inbox))
                       if os.path.isdir(os.path.join(a.inbox, d)) and not d.endswith(" - DONE")
                       and not d.startswith("_")]
            if not pending:
                print(json.dumps({"ok": True, "message": "No hay proyectos pendientes"}))
                return
            results = []
            for p in pending:
                proj = os.path.basename(p)
                log(f"=== Proyecto: {proj}")
                a.name = proj
                try:
                    r = run_project(a, [p], os.path.join(out_root, proj))
                    if a.mark_done:
                        os.rename(p, p + " - DONE")
                        r["marked_done"] = True
                except VeditError as e:
                    r = {"ok": False, "project": proj, "error": str(e)}
                results.append(r)
            print(json.dumps({"ok": all(r["ok"] for r in results), "projects": results},
                             ensure_ascii=False, indent=2))
            return
        if not a.inputs:
            raise VeditError("Indica una carpeta/videos o usa --inbox.")
        first = os.path.normpath(a.inputs[0])
        a.name = a.name or os.path.splitext(os.path.basename(first))[0]
        outdir = a.outdir or (first + "_autoedit" if os.path.isdir(first)
                              else os.path.join(os.path.dirname(first), a.name + "_autoedit"))
        res = run_project(a, a.inputs, outdir)
        if a.mark_done and os.path.isdir(first):
            os.rename(first, first + " - DONE")
            res["marked_done"] = True
        print(json.dumps(res, ensure_ascii=False, indent=2))
    except (VeditError, RuntimeError) as e:
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False, indent=2))
        sys.exit(1)


if __name__ == "__main__":
    main()
