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
from .edit import (ENC, PRESETS, VeditError, esc_path, ff, find_font, probe,
                   reframe_filter)

ORDER_LABELS = {"EXCELENTE": 3, "BUENO": 2, "ACEPTABLE": 1, "DESCARTAR": 0}


def log(msg):
    print(msg, file=sys.stderr, flush=True)


# ------------------------------------------------------------------ analysis (with cache)

def file_key(p):
    st = os.stat(p)
    return f"{os.path.abspath(p)}|{st.st_size}|{int(st.st_mtime)}"


def analyze_clips(videos, outdir, window, fps):
    cache_path = os.path.join(outdir, "quality.json")
    cache = {}
    if os.path.exists(cache_path):
        try:
            with open(cache_path, encoding="utf-8") as fh:
                cache = {c["_key"]: c for c in json.load(fh) if "_key" in c}
        except (ValueError, KeyError):
            cache = {}
    results = []
    for i, v in enumerate(videos, 1):
        key = f"{file_key(v)}|{window}|{fps}"
        if key in cache:
            results.append(cache[key])
            continue
        log(f"[{i}/{len(videos)}] Calidad: {os.path.basename(v)}")
        try:
            r = Q.measure(v, fps=fps, window=window)
        except RuntimeError as e:
            r = {"file": os.path.basename(v), "path": os.path.abspath(v), "error": str(e),
                 "score": 0, "label": "ERROR"}
        r["_key"] = key
        results.append(r)
        with open(cache_path, "w", encoding="utf-8") as fh:  # guardado incremental
            json.dump(results, fh, ensure_ascii=False, indent=1)
    return results


# ------------------------------------------------------------------ vision rating

def middle_frame(path, t, out):
    ff("-ss", f"{t:.3f}", "-i", path, "-frames:v", "1", "-vf", "scale=480:-2", "-q:v", "3", out)
    return out


def vision_rate(cands, a, tmp):
    """Pide al modelo un puntaje 1-10 de atractivo visual por toma."""
    from . import analyze as A
    frames = []
    for i, c in enumerate(cands, 1):
        seg = c["segment"]
        p = middle_frame(c["path"], (seg[0] + seg[1]) / 2, os.path.join(tmp, f"v{i:03d}.jpg"))
        frames.append({"path": p, "t": (seg[0] + seg[1]) / 2, "n": i})
    ns = types.SimpleNamespace(model=a.model, timeout=600)
    content = [{"type": "text", "text": (
        f"Estas grillas muestran {len(frames)} tomas numeradas (#n) candidatas para un video"
        f"{' sobre: ' + a.topic if a.topic else ''}. Puntua cada toma del 1 al 10 segun que tan "
        "atractiva, clara e interesante es para el video final (composicion, sujeto, emocion). "
        "Responde SOLO un JSON como {\"1\": 7, \"2\": 4}.")}]
    content += [A.img_part(s) for s in A.contact_sheets(frames)]
    try:
        ans = A.chat(ns, content, max_tokens=600)
        import re
        m = re.search(r"\{.*\}", ans, re.S)
        scores = {int(k): float(v) for k, v in json.loads(m.group(0)).items()} if m else {}
    except Exception as e:  # el modelo es opcional: si falla seguimos solo con calidad
        log(f"Vision no disponible ({e}); sigo solo con calidad tecnica.")
        return
    for i, c in enumerate(cands, 1):
        if i in scores:
            c["vision"] = max(1.0, min(10.0, scores[i]))


# ------------------------------------------------------------------ selection

def signature(path, t, tmp, idx):
    from PIL import Image
    p = middle_frame(path, t, os.path.join(tmp, f"s{idx:03d}.jpg"))
    with Image.open(p) as im:
        return list(im.convert("L").resize((16, 16)).tobytes())


def select(results, a, tmp):
    min_rank = ORDER_LABELS[a.min_label]
    cands = []
    for r in results:
        if "error" in r or ORDER_LABELS.get(r["label"], 0) < min_rank:
            continue
        b = r["best_segment"]
        s, e = b["start"], min(b["end"], b["start"] + a.clip_max)
        if e - s < a.clip_min:
            continue
        cands.append({**r, "segment": [round(s, 2), round(e, 2)]})
    if not cands:
        return [], []
    if a.vision:
        vision_rate(cands, a, tmp)
    for c in cands:
        c["final"] = round(c["score"] * 0.6 + c["vision"] * 4, 1) if "vision" in c \
            else c["score"]
    cands.sort(key=lambda c: c["final"], reverse=True)

    # quitar tomas casi identicas
    kept, sigs, dropped = [], [], []
    for i, c in enumerate(cands):
        sig = signature(c["path"], sum(c["segment"]) / 2, tmp, i)
        if any(sum(abs(x - y) for x, y in zip(sig, o)) / 256 < a.similar for o in sigs):
            dropped.append({**c, "dropped": "toma repetida"})
            continue
        sigs.append(sig)
        kept.append(c)

    chosen, total = [], 0.0
    for c in kept:
        d = c["segment"][1] - c["segment"][0]
        if total >= a.target:
            break
        if total + d - a.transition > a.max_target:
            d = a.max_target - total + a.transition
            if d < a.clip_min:
                continue
            c["segment"][1] = round(c["segment"][0] + d, 2)
        chosen.append(c)
        total += d - (a.transition if chosen[1:] else 0)
    if a.order == "name":
        chosen.sort(key=lambda c: c["file"].lower())
    elif a.order == "time":
        chosen.sort(key=lambda c: os.path.getmtime(c["path"]))
    return chosen, dropped


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
        chain.append(
            f"{vout}drawtext=fontfile={esc_path(find_font(a.font))}:textfile={esc_path(tf)}:"
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
        mus = (f"[{mi}:a]volume={a.music_volume},atrim=0:{total:.3f},afade=t=in:d=1,"
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
    ff(*inputs, "-filter_complex", ";".join(chain), "-map", vout, "-map", aout, *v,
       "-c:a", "aac", "-b:a", "192k", "-t", f"{total:.3f}", "-movflags", "+faststart", final)
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
         "## Linea de tiempo", "", "| # | Clip | Tramo | Calidad | Vision | Puntaje final |",
         "|---|---|---|---|---|---|"]
    for i, c in enumerate(chosen, 1):
        L.append(f"| {i} | {c['file']} | {c['segment'][0]}-{c['segment'][1]}s | "
                 f"{c['score']} ({c['label']}) | {c.get('vision', '-')} | {c['final']} |")
    L += ["", "## Calidad de todos los clips", "", Q.to_markdown(ok)]
    errs = [r for r in results if "error" in r]
    if errs or dropped:
        L += ["", "## Descartados", ""]
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
    results = analyze_clips(videos, outdir, a.clip_max, a.fps)
    with tempfile.TemporaryDirectory() as tmp:
        chosen, dropped = select(results, a, tmp)
        if not chosen:
            raise VeditError(f"Ningun clip alcanza el nivel minimo '{a.min_label}'. "
                             "Prueba --min-label DESCARTAR o revisa quality.json.")
        final, total = None, sum(c["segment"][1] - c["segment"][0] for c in chosen)
        if not a.dry_run:
            final, _, total = render(chosen, a, outdir, tmp)
    title = a.name or "autoedit"
    timeline = {"generated": datetime.now().isoformat(), "preset": a.preset,
                "transition": a.transition, "total_duration": round(total, 2),
                "output": final,
                "clips": [{"file": c["file"], "path": c["path"], "in": c["segment"][0],
                           "out": c["segment"][1], "quality": c["score"],
                           "vision": c.get("vision"), "final": c["final"]} for c in chosen]}
    with open(os.path.join(outdir, "timeline.json"), "w", encoding="utf-8") as fh:
        json.dump(timeline, fh, ensure_ascii=False, indent=2)
    write_edl(chosen, os.path.join(outdir, "timeline.edl"), title)
    report = os.path.join(outdir, "reporte.md")
    write_report(report, a, results, chosen, dropped, final, total)
    res = {"ok": True, "output": final, "duration": round(total, 2), "clips_used": len(chosen),
           "clips_analyzed": len(results), "report": report,
           "timeline": os.path.join(outdir, "timeline.json"),
           "edl": os.path.join(outdir, "timeline.edl")}
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
    ap.add_argument("--similar", type=float, default=8.0, help="umbral de tomas repetidas")
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
