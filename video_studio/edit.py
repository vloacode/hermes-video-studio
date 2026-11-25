"""video_studio.edit - editor de video por linea de comandos para agentes.

Parte de hermes-video-studio (AGPL-3.0). Solo depende de FFmpeg (faster-whisper opcional). Cada comando escribe un archivo NUEVO (nunca toca el original), verifica el
resultado con ffprobe e imprime un JSON con el resumen.

Uso: vstudio edit <comando> [opciones]    (vstudio edit -h para la lista)
"""
import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile

ENC = os.environ.get("VEDIT_ENCODER", "libx264")  # h264_qsv (Intel), h264_nvenc (NVIDIA)
NULL = "NUL" if os.name == "nt" else "/dev/null"

PRESETS = {  # nombre: (ancho, alto, fps, bitrate video, nota)
    "youtube": (1920, 1080, 30, "10M", "16:9 1080p"),
    "youtube-4k": (3840, 2160, 30, "35M", "16:9 4K"),
    "shorts": (1080, 1920, 30, "8M", "9:16 YouTube Shorts"),
    "reels": (1080, 1920, 30, "8M", "9:16 Instagram Reels"),
    "tiktok": (1080, 1920, 30, "8M", "9:16 TikTok"),
    "instagram-feed": (1080, 1350, 30, "6M", "4:5 feed"),
    "square": (1080, 1080, 30, "6M", "1:1"),
    "whatsapp": (1280, 720, 30, "2M", "16:9 liviano"),
}
ASPECTS = {"16:9": (16, 9), "9:16": (9, 16), "1:1": (1, 1), "4:5": (4, 5), "4:3": (4, 3)}
POS = {  # posiciones para texto/logos (x, y) con margen m
    "top-left": ("{m}", "{m}"), "top": ("(W-w)/2", "{m}"), "top-right": ("W-w-{m}", "{m}"),
    "center": ("(W-w)/2", "(H-h)/2"), "bottom-left": ("{m}", "H-h-{m}"),
    "bottom": ("(W-w)/2", "H-h-{m}"), "bottom-right": ("W-w-{m}", "H-h-{m}"),
}


# ------------------------------------------------------------------ helpers

class VeditError(Exception):
    pass


def run(cmd, check=True, cwd=None):
    r = subprocess.run(cmd, capture_output=True, text=True, errors="replace", cwd=cwd)
    if check and r.returncode != 0:
        raise VeditError(f"ffmpeg fallo:\n{r.stderr[-2000:]}")
    return r


def ff(*args, cwd=None):
    return run(["ffmpeg", "-hide_banner", "-v", "error", "-y", *args], cwd=cwd)


def stage_assets(tmp, **files):
    """Copia fuente/texto/subtitulos a una carpeta temporal con nombres simples.
    En Windows las rutas con 'C:' y espacios dentro de un filtro de FFmpeg fallan en
    silencio; con cwd=tmp y nombres relativos no hay nada que escapar."""
    out = {}
    for key, src in files.items():
        name = key + os.path.splitext(src)[1].lower()
        shutil.copyfile(src, os.path.join(tmp, name))
        out[key] = name
    return out


def frame_diff(a, b, t):
    """Mayor diferencia por bloque entre el fotograma t de dos videos. El ruido de
    recompresion da valores bajos (<2); un texto o logo nuevo da un bloque alto."""
    W, H, B = 640, 360, 8

    def grab(p):
        r = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.3f}", "-i", os.path.abspath(p),
                            "-frames:v", "1", "-vf", f"scale={W}:{H},format=gray", "-f",
                            "rawvideo", "-"], capture_output=True)
        return r.stdout
    x, y = grab(a), grab(b)
    if len(x) != W * H or len(y) != W * H:
        return None
    best = 0.0
    for by in range(0, H, B):
        for bx in range(0, W, B):
            tot = 0
            for row in range(by, by + B):
                i = row * W + bx
                tot += sum(abs(p - q) for p, q in zip(x[i:i + B], y[i:i + B]))
            best = max(best, tot / (B * B))
    return best


def probe(path):
    if not os.path.isfile(path):
        raise VeditError(f"No existe: {path}")
    r = run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format",
             "-show_streams", path])
    d = json.loads(r.stdout)
    v = next((s for s in d["streams"] if s["codec_type"] == "video"), None)
    a = next((s for s in d["streams"] if s["codec_type"] == "audio"), None)
    fps = None
    if v and v.get("avg_frame_rate", "0/0") != "0/0":
        n, dd = v["avg_frame_rate"].split("/")
        fps = round(int(n) / int(dd), 3) if int(dd) else None
    return {"duration": float(d["format"].get("duration") or 0),
            "size_mb": round(int(d["format"].get("size", 0)) / 1048576, 2),
            "width": v and v.get("width"), "height": v and v.get("height"), "fps": fps,
            "video_codec": v and v.get("codec_name"), "has_audio": a is not None,
            "audio_codec": a and a.get("codec_name")}


def out_name(src, tag, ext=None, out=None):
    if out:
        return out
    stem, e = os.path.splitext(src)
    return f"{stem}_{tag}{ext or e or '.mp4'}"


def venc(crf=20):
    if ENC == "libx264":
        return ["-c:v", "libx264", "-preset", "medium", "-crf", str(crf), "-pix_fmt", "yuv420p"]
    return ["-c:v", ENC, "-b:v", "8M", "-pix_fmt", "yuv420p"]


AENC = ["-c:a", "aac", "-b:a", "192k"]


def done(output, **extra):
    info = probe(output)
    if info["duration"] <= 0 and not output.lower().endswith((".png", ".jpg", ".wav",
                                                               ".mp3", ".srt")):
        raise VeditError(f"El resultado parece vacio: {output}")
    res = {"ok": True, "output": os.path.abspath(output), **info, **extra}
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return res


def esc_path(p):
    """Ruta segura dentro de un filtro de ffmpeg (Windows incluido)."""
    p = os.path.abspath(p).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
    return f"'{p}'"


def find_font(font=None):
    cands = [font] if font else []
    cands += [r"C:\Windows\Fonts\arialbd.ttf", r"C:\Windows\Fonts\arial.ttf",
              r"C:\Windows\Fonts\segoeui.ttf", "/System/Library/Fonts/Helvetica.ttc",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]
    for c in cands:
        if c and os.path.isfile(c):
            return c
    raise VeditError("No encontre una fuente; pasa --font <archivo .ttf>.")


def tsec(v):
    """Acepta 12.5, '1:02', '00:01:02.5'."""
    if v is None:
        return None
    s = str(v).strip()
    if ":" in s:
        parts = [float(x) for x in s.split(":")]
        return sum(p * 60 ** i for i, p in enumerate(reversed(parts)))
    return float(s)


def atempo_chain(f):
    steps = []
    while f > 2.0:
        steps.append(2.0)
        f /= 2.0
    while f < 0.5:
        steps.append(0.5)
        f /= 0.5
    steps.append(f)
    return ",".join(f"atempo={s:.4f}" for s in steps)


def has_filter(name):
    return name in run(["ffmpeg", "-hide_banner", "-filters"], check=False).stdout


# ------------------------------------------------------------------ inspect

def c_info(a):
    print(json.dumps({"input": os.path.abspath(a.input), **probe(a.input)}, indent=2))


def detect_silences(path, db, min_d):
    r = run(["ffmpeg", "-hide_banner", "-i", path, "-af",
             f"silencedetect=noise={db}dB:d={min_d}", "-f", "null", "-"], check=False)
    starts = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", r.stderr)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", r.stderr)]
    dur = probe(path)["duration"]
    return [(s, ends[i] if i < len(ends) else dur) for i, s in enumerate(starts)]


def c_silences(a):
    sil = detect_silences(a.input, a.db, a.min)
    print(json.dumps({"silences": [{"start": round(s, 2), "end": round(e, 2)}
                                   for s, e in sil]}, indent=2))


def c_scenes(a):
    r = run(["ffmpeg", "-hide_banner", "-i", a.input, "-vf",
             f"select='gt(scene,{a.threshold})',showinfo", "-an", "-f", "null", "-"],
            check=False)
    ts = [round(float(t), 2) for t in re.findall(r"pts_time:([\d.]+)", r.stderr)]
    print(json.dumps({"scene_changes": ts}, indent=2))


def c_frames(a):
    d = probe(a.input)["duration"]
    os.makedirs(a.outdir, exist_ok=True)
    outs = []
    for i in range(a.count):
        t = d * (i + 0.5) / a.count
        o = os.path.join(a.outdir, f"frame_{i + 1:02d}_{t:07.2f}s.jpg")
        ff("-ss", f"{t:.3f}", "-i", a.input, "-frames:v", "1", "-q:v", "2", o)
        outs.append(o)
    print(json.dumps({"ok": True, "frames": [os.path.abspath(o) for o in outs]}, indent=2))


def c_sheet(a):
    d = probe(a.input)["duration"]
    n = a.cols * a.rows
    fps = n / d if d else 1
    o = out_name(a.input, "sheet", ".jpg", a.output)
    ff("-i", a.input, "-vf", f"fps={fps:.5f},scale=480:-2,tile={a.cols}x{a.rows}",
       "-frames:v", "1", "-q:v", "3", o)
    print(json.dumps({"ok": True, "output": os.path.abspath(o)}, indent=2))


def c_loudness(a):
    r = run(["ffmpeg", "-hide_banner", "-i", a.input, "-af",
             "loudnorm=print_format=json", "-f", "null", "-"], check=False)
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", r.stderr, re.S)
    if not m:
        raise VeditError("No pude medir el volumen (¿el video tiene audio?).")
    j = json.loads(m.group(0))
    print(json.dumps({"integrated_lufs": float(j["input_i"]), "true_peak_db": float(j["input_tp"]),
                      "range_lu": float(j["input_lra"]),
                      "nota": "YouTube/Reels/TikTok apuntan a ~-14 LUFS"}, indent=2))


# ------------------------------------------------------------------ cut & time

def c_trim(a):
    s, e = tsec(a.start) or 0, tsec(a.end)
    o = out_name(a.input, "trim", out=a.output)
    args = ["-ss", f"{s:.3f}", "-i", a.input]
    if e is not None:
        args += ["-t", f"{e - s:.3f}"]
    ff(*args, *venc(), *AENC, "-movflags", "+faststart", o)
    done(o)


def c_split(a):
    d = probe(a.input)["duration"]
    cuts = [0.0] + sorted(tsec(x) for x in a.at.split(",")) + [d]
    stem, ext = os.path.splitext(a.input)
    outs = []
    for i in range(len(cuts) - 1):
        o = f"{stem}_part{i + 1}{ext or '.mp4'}"
        ff("-ss", f"{cuts[i]:.3f}", "-i", a.input, "-t", f"{cuts[i + 1] - cuts[i]:.3f}",
           *venc(), *AENC, o)
        outs.append(os.path.abspath(o))
    print(json.dumps({"ok": True, "parts": outs}, indent=2))


def c_join(a):
    """Une clips re-codificando a la resolucion del primero (seguro con fuentes mixtas)."""
    infos = [probe(p) for p in a.inputs]
    w, h = infos[0]["width"], infos[0]["height"]
    fps = a.fps or infos[0]["fps"] or 30
    args, parts = [], []
    for i, (p, inf) in enumerate(zip(a.inputs, infos)):
        args += ["-i", p]
        if not inf["has_audio"]:
            args += ["-f", "lavfi", "-t", f"{inf['duration']:.3f}", "-i",
                     "anullsrc=r=48000:cl=stereo"]
    idx = 0
    filt = []
    for i, inf in enumerate(infos):
        vi = idx
        ai = idx if inf["has_audio"] else idx + 1
        idx += 1 if inf["has_audio"] else 2
        filt.append(f"[{vi}:v]scale={w}:{h}:force_original_aspect_ratio=decrease,"
                    f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps}[v{i}]")
        filt.append(f"[{ai}:a]aresample=48000,aformat=channel_layouts=stereo[a{i}]")
        parts.append(f"[v{i}][a{i}]")
    filt.append("".join(parts) + f"concat=n={len(infos)}:v=1:a=1[v][a]")
    o = a.output or out_name(a.inputs[0], "joined")
    ff(*args, "-filter_complex", ";".join(filt), "-map", "[v]", "-map", "[a]",
       *venc(), *AENC, "-movflags", "+faststart", o)
    done(o, clips=len(infos))


def c_remove_silence(a):
    info = probe(a.input)
    if not info["has_audio"]:
        raise VeditError("El video no tiene audio; no hay silencios que detectar.")
    sil = detect_silences(a.input, a.db, a.min)
    d = info["duration"]
    keep, cur = [], 0.0
    for s, e in sil:
        s2, e2 = s + a.pad, e - a.pad
        if s2 > cur:
            keep.append((cur, s2))
        cur = max(cur, e2)
    if cur < d:
        keep.append((cur, d))
    keep = [(s, e) for s, e in keep if e - s > 0.05]
    if not sil or not keep:
        raise VeditError("No se encontraron silencios con esos parametros (prueba --db -30).")
    sel = "+".join(f"between(t,{s:.3f},{e:.3f})" for s, e in keep)
    o = out_name(a.input, "nosilence", out=a.output)
    ff("-i", a.input, "-vf", f"select='{sel}',setpts=N/FRAME_RATE/TB",
       "-af", f"aselect='{sel}',asetpts=N/SR/TB", *venc(), *AENC, o)
    removed = d - sum(e - s for s, e in keep)
    done(o, segments_kept=len(keep), seconds_removed=round(removed, 2))


def c_speed(a):
    f = a.factor
    o = out_name(a.input, f"x{f:g}", out=a.output)
    args = ["-i", a.input, "-vf", f"setpts=PTS/{f}"]
    args += ["-af", atempo_chain(f)] if probe(a.input)["has_audio"] else ["-an"]
    ff(*args, *venc(), *AENC, o)
    done(o, factor=f)


def c_reverse(a):
    o = out_name(a.input, "reverse", out=a.output)
    args = ["-i", a.input, "-vf", "reverse"]
    args += ["-af", "areverse"] if probe(a.input)["has_audio"] else []
    ff(*args, *venc(), *AENC, o)
    done(o)


def c_loop(a):
    o = out_name(a.input, f"loop{a.times}", out=a.output)
    ff("-stream_loop", str(a.times - 1), "-i", a.input, *venc(), *AENC, o)
    done(o, times=a.times)


# ------------------------------------------------------------------ picture

def reframe_filter(w, h, mode):
    if mode == "crop":
        return (f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},setsar=1")
    if mode == "blur":  # fondo desenfocado del mismo video, tipico para pasar 16:9 a 9:16
        return (f"split[b][f];[b]scale={w}:{h}:force_original_aspect_ratio=increase,"
                f"crop={w}:{h},boxblur=20:2[bg];[f]scale={w}:{h}:"
                f"force_original_aspect_ratio=decrease[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2,"
                "setsar=1")
    return (f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1")


def c_reframe(a):
    info = probe(a.input)
    rw, rh = ASPECTS[a.aspect]
    h = a.height or (1920 if rh > rw else 1080)
    w = int(round(h * rw / rh / 2) * 2)
    o = out_name(a.input, a.aspect.replace(":", "x"), out=a.output)
    filt = reframe_filter(w, h, a.mode)
    ff("-i", a.input, "-filter_complex", f"[0:v]{filt}[v]", "-map", "[v]",
       *(["-map", "0:a"] if info["has_audio"] else []), *venc(), *AENC, o)
    done(o, mode=a.mode)


def c_resize(a):
    o = out_name(a.input, f"{a.width}w", out=a.output)
    ff("-i", a.input, "-vf", f"scale={a.width}:-2", *venc(), "-c:a", "copy", o)
    done(o)


def c_crop(a):
    o = out_name(a.input, "crop", out=a.output)
    ff("-i", a.input, "-vf", f"crop={a.w}:{a.h}:{a.x}:{a.y}", *venc(), "-c:a", "copy", o)
    done(o)


def c_color(a):
    o = out_name(a.input, "color", out=a.output)
    f = (f"eq=brightness={a.brightness}:contrast={a.contrast}:saturation={a.saturation}"
         f":gamma={a.gamma}")
    if a.sharpen:
        f += ",unsharp=5:5:0.8"
    ff("-i", a.input, "-vf", f, *venc(), "-c:a", "copy", o)
    done(o)


def c_denoise(a):
    o = out_name(a.input, "denoise", out=a.output)
    ff("-i", a.input, "-vf", f"hqdn3d={a.strength}:{a.strength}:{a.strength * 1.5}:"
       f"{a.strength * 1.5}", *venc(), "-c:a", "copy", o)
    done(o)


def c_stabilize(a):
    o = out_name(a.input, "stable", out=a.output)
    if has_filter("vidstabdetect"):
        with tempfile.TemporaryDirectory() as tmp:
            trf = os.path.join(tmp, "t.trf")
            src, o = os.path.abspath(a.input), os.path.abspath(o)
            run(["ffmpeg", "-hide_banner", "-v", "error", "-y", "-i", src, "-vf",
                 f"vidstabdetect=shakiness={a.shakiness}:result=t.trf",
                 "-f", "null", "-"], cwd=tmp)
            ff("-i", src, "-vf", f"vidstabtransform=input=t.trf:smoothing="
               f"{a.smoothing},unsharp=5:5:0.8", *venc(), "-c:a", "copy", o, cwd=tmp)
        method = "vidstab"
    else:
        ff("-i", a.input, "-vf", "deshake", *venc(), "-c:a", "copy", o)
        method = "deshake"
    done(o, method=method)


# ------------------------------------------------------------------ overlays

def xy(pos, margin, text=False):
    """Expresiones x,y. En overlay W/H = video y w/h = logo; en drawtext W/H/w/h son
    TODOS el video y el tamano del texto es text_w/text_h."""
    x, y = (e.format(m=margin) for e in POS[pos])
    if text:
        x = x.replace("W-w", "W-text_w").replace("(W-w)", "(W-text_w)")
        y = y.replace("H-h", "H-text_h").replace("(H-h)", "(H-text_h)")
    return x, y


def c_text(a):
    o = os.path.abspath(out_name(a.input, "text", out=a.output))
    src = os.path.abspath(a.input)
    font = find_font(a.font)
    with tempfile.TemporaryDirectory() as tmp:
        tf = os.path.join(tmp, "src_text.txt")
        with open(tf, "w", encoding="utf-8") as fh:
            fh.write(a.text)
        assets = stage_assets(tmp, font=font, text=tf)
        x, y = xy(a.position, a.margin, text=True)
        f = (f"drawtext=fontfile={assets['font']}:textfile={assets['text']}:fontsize={a.size}:"
             f"fontcolor={a.color}:borderw={a.border}:bordercolor=black@0.8:x={x}:y={y}")
        if a.box:
            f += ":box=1:boxcolor=black@0.5:boxborderw=12"
        s, e = tsec(a.start), tsec(a.end)
        if s is not None or e is not None:
            f += f":enable='between(t,{s or 0},{e if e is not None else 1e9})'"
        ff("-i", src, "-vf", f, *venc(), "-c:a", "copy", o, cwd=tmp)
    # Verificar que el texto realmente se dibujo
    t_chk = (s or 0) + 0.5 if (s is not None or e is not None) else min(1.0, probe(src)["duration"] / 2)
    d = frame_diff(src, o, t_chk)
    if d is not None and d < 2.5:
        raise VeditError("FFmpeg termino pero el texto NO aparece en el video "
                         f"(diferencia {d:.2f}). Revisa --font, --size y --position.")
    done(o, text_verified=d is not None)


def srt_time(t):
    h, r = divmod(t, 3600)
    m, s = divmod(r, 60)
    return f"{int(h):02d}:{int(m):02d}:{int(s):02d},{int(round((s % 1) * 1000)):03d}"


def c_captions(a):
    """Transcribe con faster-whisper -> .srt; con --burn los quema en el video."""
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise VeditError("Instala faster-whisper: pip install faster-whisper")
    if not probe(a.input)["has_audio"]:
        raise VeditError("El video no tiene audio.")
    with tempfile.TemporaryDirectory() as tmp:
        wav = os.path.join(tmp, "a.wav")
        ff("-i", a.input, "-vn", "-ac", "1", "-ar", "16000", wav)
        model = WhisperModel(a.whisper_model, device="cpu", compute_type="int8")
        segs, info = model.transcribe(wav, language=a.language, vad_filter=True,
                                      word_timestamps=a.max_words > 0)
        lines = []
        for s in segs:
            if a.max_words and s.words:  # subtitulos cortos estilo reels
                ws = list(s.words)
                for i in range(0, len(ws), a.max_words):
                    g = ws[i:i + a.max_words]
                    lines.append((g[0].start, g[-1].end, "".join(w.word for w in g).strip()))
            else:
                lines.append((s.start, s.end, s.text.strip()))
    srt = out_name(a.input, "captions", ".srt", a.srt)
    with open(srt, "w", encoding="utf-8") as fh:
        for i, (s, e, t) in enumerate(lines, 1):
            fh.write(f"{i}\n{srt_time(s)} --> {srt_time(e)}\n{t}\n\n")
    res = {"ok": True, "srt": os.path.abspath(srt), "lines": len(lines),
           "language": info.language}
    if not a.burn:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return
    a.subs, a.output = srt, a.output
    c_subtitles(a)


def c_subtitles(a):
    o = os.path.abspath(out_name(a.input, "subs", out=a.output))
    src = os.path.abspath(a.input)
    style = (f"FontName={a.font_name},FontSize={a.size},PrimaryColour=&H00FFFFFF,"
             f"OutlineColour=&H00000000,BorderStyle=1,Outline=2,Shadow=0,Alignment=2,"
             f"MarginV={a.margin_v}")
    with tempfile.TemporaryDirectory() as tmp:
        assets = stage_assets(tmp, subs=a.subs)
        ff("-i", src, "-vf", f"subtitles={assets['subs']}:force_style='{style}'",
           *venc(), "-c:a", "copy", o, cwd=tmp)
    done(o)


def c_overlay(a):
    """Logo/imagen o video encima (picture-in-picture)."""
    o = out_name(a.input, "overlay", out=a.output)
    W = probe(a.input)["width"]
    ow = int(W * a.scale)
    x, y = xy(a.position, a.margin)
    en = ""
    s, e = tsec(a.start), tsec(a.end)
    if s is not None or e is not None:
        en = f":enable='between(t,{s or 0},{e if e is not None else 1e9})'"
    filt = (f"[1:v]scale={ow}:-1,format=rgba,colorchannelmixer=aa={a.opacity}[ov];"
            f"[0:v][ov]overlay={x}:{y}{en}:eof_action=pass[v]")
    loop = ["-loop", "1"] if a.overlay.lower().endswith((".png", ".jpg", ".jpeg", ".webp")) else []
    ff("-i", a.input, *loop, "-i", a.overlay, "-filter_complex", filt, "-map", "[v]",
       "-map", "0:a?", "-t", f"{probe(a.input)['duration']:.3f}", *venc(), *AENC, o)
    done(o)


def c_blur(a):
    o = out_name(a.input, "blur", out=a.output)
    en = ""
    s, e = tsec(a.start), tsec(a.end)
    if s is not None or e is not None:
        en = f":enable='between(t,{s or 0},{e if e is not None else 1e9})'"
    filt = (f"[0:v]split[a][b];[b]crop={a.w}:{a.h}:{a.x}:{a.y},boxblur={a.strength}[bl];"
            f"[a][bl]overlay={a.x}:{a.y}{en}[v]")
    ff("-i", a.input, "-filter_complex", filt, "-map", "[v]", "-map", "0:a?",
       *venc(), "-c:a", "copy", o)
    done(o)


# ------------------------------------------------------------------ audio

def c_extract_audio(a):
    o = out_name(a.input, "audio", "." + a.format, a.output)
    codec = {"wav": ["-c:a", "pcm_s16le"], "mp3": ["-c:a", "libmp3lame", "-q:a", "2"],
             "m4a": AENC}[a.format]
    ff("-i", a.input, "-vn", *codec, o)
    done(o)


def c_replace_audio(a):
    o = out_name(a.input, "newaudio", out=a.output)
    ff("-i", a.input, "-i", a.audio, "-map", "0:v", "-map", "1:a", "-c:v", "copy",
       *AENC, "-shortest", o)
    done(o)


def c_music(a):
    """Agrega musica de fondo; con --duck baja la musica cuando hay voz."""
    info = probe(a.input)
    o = out_name(a.input, "music", out=a.output)
    d = info["duration"]
    mus = (f"[1:a]volume={a.volume},afade=t=in:d=1,afade=t=out:st={max(d - a.fade, 0):.2f}:"
           f"d={a.fade},atrim=0:{d:.3f}[m]")
    if not info["has_audio"]:
        filt = mus + ";[m]anull[aout]"
    elif a.duck:
        filt = (mus + ";[0:a]asplit[v1][v2];[m][v1]sidechaincompress=threshold=0.05:"
                "ratio=8:attack=20:release=400[md];[v2][md]amix=inputs=2:duration=first:"
                "dropout_transition=0[aout]")
    else:
        filt = mus + ";[0:a][m]amix=inputs=2:duration=first:dropout_transition=0[aout]"
    ff("-i", a.input, "-stream_loop", "-1", "-i", a.music, "-filter_complex", filt,
       "-map", "0:v", "-map", "[aout]", "-c:v", "copy", *AENC, "-t", f"{d:.3f}", o)
    done(o, ducking=a.duck)


def c_normalize(a):
    o = out_name(a.input, "loud", out=a.output)
    ff("-i", a.input, "-af", f"loudnorm=I={a.lufs}:TP=-1.5:LRA=11", "-c:v", "copy", *AENC, o)
    done(o, target_lufs=a.lufs)


def c_volume(a):
    o = out_name(a.input, "vol", out=a.output)
    ff("-i", a.input, "-af", f"volume={a.factor}", "-c:v", "copy", *AENC, o)
    done(o)


def c_fade(a):
    info = probe(a.input)
    d = info["duration"]
    o = out_name(a.input, "fade", out=a.output)
    vf, af = [], []
    if a.fade_in:
        vf.append(f"fade=t=in:st=0:d={a.fade_in}")
        af.append(f"afade=t=in:st=0:d={a.fade_in}")
    if a.fade_out:
        st = max(d - a.fade_out, 0)
        vf.append(f"fade=t=out:st={st:.3f}:d={a.fade_out}")
        af.append(f"afade=t=out:st={st:.3f}:d={a.fade_out}")
    args = ["-i", a.input, "-vf", ",".join(vf or ["null"])]
    if info["has_audio"]:
        args += ["-af", ",".join(af or ["anull"])]
    ff(*args, *venc(), *AENC, o)
    done(o)


# ------------------------------------------------------------------ export

def c_export(a):
    w, h, fps, br, note = PRESETS[a.preset]
    info = probe(a.input)
    o = out_name(a.input, a.preset, ".mp4", a.output)
    filt = reframe_filter(w, h, a.mode) + f",fps={fps}"
    v = ["-c:v", "libx264", "-preset", "slow", "-crf", "19", "-maxrate", br,
         "-bufsize", br, "-pix_fmt", "yuv420p", "-profile:v", "high"] if ENC == "libx264" \
        else ["-c:v", ENC, "-b:v", br, "-pix_fmt", "yuv420p"]
    au = ["-af", "loudnorm=I=-14:TP=-1.5:LRA=11", "-c:a", "aac", "-b:a", "192k",
          "-ar", "48000"] if info["has_audio"] else []
    ff("-i", a.input, "-filter_complex", f"[0:v]{filt}[v]", "-map", "[v]",
       *(["-map", "0:a"] if info["has_audio"] else []), *v, *au,
       "-movflags", "+faststart", o)
    done(o, preset=a.preset, note=note)


def c_compress(a):
    """Comprime a un tamano objetivo en MB con 2 pasadas (para WhatsApp, email, etc.)."""
    info = probe(a.input)
    d = info["duration"]
    abr = 128 if info["has_audio"] else 0
    vbr = int(a.mb * 8192 * 0.95 / d) - abr
    if vbr < 100:
        raise VeditError(f"{a.mb} MB es muy poco para {d:.0f}s de video.")
    o = out_name(a.input, f"{a.mb:g}MB", ".mp4", a.output)
    scale = ["-vf", f"scale='min({a.max_width},iw)':-2"]
    with tempfile.TemporaryDirectory() as tmp:
        log = os.path.join(tmp, "pass")
        ff("-i", a.input, *scale, "-c:v", "libx264", "-b:v", f"{vbr}k", "-pass", "1",
           "-passlogfile", log, "-an", "-f", "mp4", NULL)
        ff("-i", a.input, *scale, "-c:v", "libx264", "-b:v", f"{vbr}k", "-pass", "2",
           "-passlogfile", log, *(["-c:a", "aac", "-b:a", f"{abr}k"] if abr else ["-an"]),
           "-movflags", "+faststart", o)
    done(o, target_mb=a.mb, video_kbps=vbr)


def c_gif(a):
    o = out_name(a.input, "gif", ".gif", a.output)
    s, e = tsec(a.start) or 0, tsec(a.end)
    rng = ["-ss", f"{s:.3f}"] + (["-t", f"{e - s:.3f}"] if e else [])
    f = f"fps={a.fps},scale={a.width}:-1:flags=lanczos"
    ff(*rng, "-i", a.input, "-vf", f"{f},split[x][y];[x]palettegen[p];[y][p]paletteuse", o)
    print(json.dumps({"ok": True, "output": os.path.abspath(o),
                      "size_mb": round(os.path.getsize(o) / 1048576, 2)}, indent=2))


def c_thumbnail(a):
    o = out_name(a.input, "thumb", ".jpg", a.output)
    if a.at is not None:
        ff("-ss", f"{tsec(a.at):.3f}", "-i", a.input, "-frames:v", "1", "-q:v", "2", o)
    else:  # el fotograma mas representativo
        ff("-i", a.input, "-vf", "thumbnail=150", "-frames:v", "1", "-q:v", "2", o)
    print(json.dumps({"ok": True, "output": os.path.abspath(o)}, indent=2))


def c_check(a):
    """Verifica si el video cumple los requisitos de una plataforma."""
    info = probe(a.input)
    w, h, fps, br, note = PRESETS[a.preset]
    issues = []
    if (info["width"], info["height"]) != (w, h):
        issues.append(f"resolucion {info['width']}x{info['height']} (esperado {w}x{h})")
    if info["video_codec"] != "h264":
        issues.append(f"codec {info['video_codec']} (recomendado h264)")
    if info["fps"] and abs(info["fps"] - fps) > 1 and info["fps"] > 60:
        issues.append(f"fps {info['fps']}")
    if a.preset in ("shorts", "reels", "tiktok") and info["duration"] > 180:
        issues.append(f"duracion {info['duration']:.0f}s (Shorts max 180s)")
    if not info["has_audio"]:
        issues.append("sin audio")
    print(json.dumps({"preset": a.preset, "ok": not issues, "issues": issues, **info},
                     indent=2))


def c_doctor(a):
    out = {"ffmpeg": shutil.which("ffmpeg"), "ffprobe": shutil.which("ffprobe"),
           "encoder": ENC}
    if out["ffmpeg"]:
        v = run(["ffmpeg", "-hide_banner", "-version"], check=False).stdout.splitlines()
        out["version"] = v[0] if v else ""
        filters = run(["ffmpeg", "-hide_banner", "-filters"], check=False).stdout
        encs = run(["ffmpeg", "-hide_banner", "-encoders"], check=False).stdout
        out["drawtext"] = "drawtext" in filters
        out["subtitles"] = " subtitles " in filters
        out["vidstab"] = "vidstabdetect" in filters
        out["h264_qsv (Intel)"] = "h264_qsv" in encs
        out["h264_nvenc (NVIDIA)"] = "h264_nvenc" in encs
    try:
        import faster_whisper  # noqa: F401
        out["faster_whisper"] = True
    except ImportError:
        out["faster_whisper"] = False
    print(json.dumps(out, indent=2))



def c_recipe(a):
    """Ejecuta varios pasos en cadena en UNA llamada; se detiene en el primer error."""
    import contextlib
    import io
    steps = a.steps
    if os.path.isfile(steps):
        with open(steps, encoding="utf-8") as fh:
            steps = fh.read()
    try:
        steps = json.loads(steps)
    except ValueError as e:
        raise VeditError(f"--steps no es JSON valido: {e}")
    if not isinstance(steps, list) or not steps:
        raise VeditError("--steps debe ser una lista de pasos, ej: "
                         '[{"op":"trim","start":5,"end":20},{"op":"text","text":"Hola"}]')
    parser = build()
    current, done_steps = a.input, []
    final = out_name(a.input, "recipe", ".mp4", a.output)
    with tempfile.TemporaryDirectory() as tmp:
        for i, st in enumerate(steps, 1):
            st = dict(st)
            op = st.pop("op", None)
            if op in (None, "recipe", "join", "doctor", "split", "frames"):
                raise VeditError(f"Paso {i}: op '{op}' no permitido en recipe")
            out = os.path.join(tmp, f"step{i:02d}.mp4")
            argv = [op, current, "-o", out]
            for k, v in st.items():
                flag = "--" + k.replace("_", "-")
                if v is True:
                    argv.append(flag)
                elif v not in (False, None):
                    argv += [flag, str(v)]
            try:
                sub = parser.parse_args(argv)
            except SystemExit:
                raise VeditError(f"Paso {i} ({op}): parametros invalidos {st}")
            buf = io.StringIO()
            try:
                with contextlib.redirect_stdout(buf):
                    sub.fn(sub)
            except VeditError as e:
                raise VeditError(f"Paso {i} ({op}) fallo: {e}. Pasos completados: "
                                 f"{[d['op'] for d in done_steps]}")
            done_steps.append({"step": i, "op": op, "params": st})
            current = out
        shutil.copyfile(current, final)
    done(final, steps_done=done_steps, steps_total=len(steps))

# ------------------------------------------------------------------ CLI

def build():
    p = argparse.ArgumentParser(prog="vstudio edit", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_, io=True):
        s = sp.add_parser(name, help=help_)
        if io:
            s.add_argument("input")
            s.add_argument("-o", "--output")
        s.set_defaults(fn=fn)
        return s

    add("doctor", c_doctor, "revisa ffmpeg, encoders y extras", io=False)
    add("info", c_info, "datos del video")
    s = add("silences", c_silences, "lista silencios")
    s.add_argument("--db", type=float, default=-35)
    s.add_argument("--min", type=float, default=0.6)
    s = add("scenes", c_scenes, "lista cambios de escena")
    s.add_argument("--threshold", type=float, default=0.3)
    s = add("frames", c_frames, "extrae N fotogramas repartidos")
    s.add_argument("--count", type=int, default=8)
    s.add_argument("--outdir", default="frames")
    s = add("sheet", c_sheet, "hoja de contacto (grilla de fotogramas)")
    s.add_argument("--cols", type=int, default=4)
    s.add_argument("--rows", type=int, default=3)
    add("loudness", c_loudness, "mide volumen en LUFS")

    s = add("trim", c_trim, "recorta entre --start y --end")
    s.add_argument("--start", default="0")
    s.add_argument("--end")
    s = add("split", c_split, "parte en varios clips")
    s.add_argument("--at", required=True, help="tiempos separados por coma: 10,1:30")
    s = sp.add_parser("join", help="une varios clips en orden")
    s.add_argument("inputs", nargs="+")
    s.add_argument("-o", "--output")
    s.add_argument("--fps", type=float)
    s.set_defaults(fn=c_join)
    s = add("remove-silence", c_remove_silence, "quita silencios (jump cuts)")
    s.add_argument("--db", type=float, default=-35)
    s.add_argument("--min", type=float, default=0.6)
    s.add_argument("--pad", type=float, default=0.12, help="aire que se deja en cada corte")
    s = add("speed", c_speed, "acelera o ralentiza (con audio)")
    s.add_argument("--factor", type=float, required=True)
    add("reverse", c_reverse, "reproduce al reves")
    s = add("loop", c_loop, "repite el clip N veces")
    s.add_argument("--times", type=int, default=2)

    s = add("reframe", c_reframe, "cambia el formato (9:16, 1:1...)")
    s.add_argument("--aspect", choices=list(ASPECTS), required=True)
    s.add_argument("--mode", choices=["crop", "pad", "blur"], default="blur")
    s.add_argument("--height", type=int)
    s = add("resize", c_resize, "cambia el ancho")
    s.add_argument("--width", type=int, required=True)
    s = add("crop", c_crop, "recorta una region")
    for k in ("w", "h", "x", "y"):
        s.add_argument(f"--{k}", type=int, required=k in "wh", default=0)
    s = add("color", c_color, "brillo/contraste/saturacion")
    s.add_argument("--brightness", type=float, default=0.0)
    s.add_argument("--contrast", type=float, default=1.0)
    s.add_argument("--saturation", type=float, default=1.0)
    s.add_argument("--gamma", type=float, default=1.0)
    s.add_argument("--sharpen", action="store_true")
    s = add("denoise", c_denoise, "reduce ruido")
    s.add_argument("--strength", type=float, default=4)
    s = add("stabilize", c_stabilize, "estabiliza video movido")
    s.add_argument("--shakiness", type=int, default=5)
    s.add_argument("--smoothing", type=int, default=15)

    s = add("text", c_text, "texto encima")
    s.add_argument("--text", required=True)
    s.add_argument("--position", choices=list(POS), default="bottom")
    s.add_argument("--size", type=int, default=64)
    s.add_argument("--color", default="white")
    s.add_argument("--border", type=int, default=3)
    s.add_argument("--box", action="store_true")
    s.add_argument("--margin", type=int, default=60)
    s.add_argument("--start")
    s.add_argument("--end")
    s.add_argument("--font")
    s = add("captions", c_captions, "subtitulos automaticos (.srt, --burn para quemarlos)")
    s.add_argument("--language", default=None, help="es, en... (vacio = detectar)")
    s.add_argument("--whisper-model", default="small")
    s.add_argument("--max-words", type=int, default=0, help="palabras por linea (reels: 3)")
    s.add_argument("--srt")
    s.add_argument("--burn", action="store_true")
    s.add_argument("--font-name", default="Arial")
    s.add_argument("--size", type=int, default=16)
    s.add_argument("--margin-v", type=int, default=40)
    s = add("subtitles", c_subtitles, "quema un .srt existente")
    s.add_argument("--subs", required=True)
    s.add_argument("--font-name", default="Arial")
    s.add_argument("--size", type=int, default=16)
    s.add_argument("--margin-v", type=int, default=40)
    s = add("overlay", c_overlay, "logo/imagen o video encima")
    s.add_argument("--overlay", required=True)
    s.add_argument("--position", choices=list(POS), default="top-right")
    s.add_argument("--scale", type=float, default=0.15, help="ancho relativo al video")
    s.add_argument("--opacity", type=float, default=1.0)
    s.add_argument("--margin", type=int, default=30)
    s.add_argument("--start")
    s.add_argument("--end")
    s = add("blur", c_blur, "difumina una region (caras, placas)")
    for k in ("w", "h", "x", "y"):
        s.add_argument(f"--{k}", type=int, required=True)
    s.add_argument("--strength", type=int, default=20)
    s.add_argument("--start")
    s.add_argument("--end")

    s = add("extract-audio", c_extract_audio, "saca el audio")
    s.add_argument("--format", choices=["wav", "mp3", "m4a"], default="mp3")
    s = add("replace-audio", c_replace_audio, "cambia el audio")
    s.add_argument("--audio", required=True)
    s = add("music", c_music, "musica de fondo")
    s.add_argument("--music", required=True)
    s.add_argument("--volume", type=float, default=0.15)
    s.add_argument("--fade", type=float, default=2.0)
    s.add_argument("--duck", action="store_true", help="baja la musica cuando hablan")
    s = add("normalize", c_normalize, "normaliza volumen (LUFS)")
    s.add_argument("--lufs", type=float, default=-14)
    s = add("volume", c_volume, "multiplica el volumen")
    s.add_argument("--factor", type=float, required=True)
    s = add("fade", c_fade, "fundidos de entrada/salida")
    s.add_argument("--fade-in", type=float, default=0.5)
    s.add_argument("--fade-out", type=float, default=0.5)

    s = add("export", c_export, "exporta para una plataforma")
    s.add_argument("--preset", choices=list(PRESETS), required=True)
    s.add_argument("--mode", choices=["crop", "pad", "blur"], default="blur")
    s = add("compress", c_compress, "comprime a N MB")
    s.add_argument("--mb", type=float, required=True)
    s.add_argument("--max-width", type=int, default=1280)
    s = add("gif", c_gif, "convierte a GIF")
    s.add_argument("--start")
    s.add_argument("--end")
    s.add_argument("--fps", type=int, default=12)
    s.add_argument("--width", type=int, default=480)
    s = add("thumbnail", c_thumbnail, "miniatura (auto o --at)")
    s.add_argument("--at")
    s = add("check", c_check, "verifica requisitos de una plataforma")
    s.add_argument("--preset", choices=list(PRESETS), required=True)
    s = add("recipe", c_recipe, "varios pasos en una sola llamada (JSON)")
    s.add_argument("--steps", required=True,
                   help='lista JSON o archivo .json: [{"op":"trim","start":5,"end":20},...]')
    return p


def main(argv=None):
    a = build().parse_args(argv)
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        sys.exit(json.dumps({"ok": False, "error": "ffmpeg/ffprobe no estan en el PATH"}))
    try:
        a.fn(a)
    except VeditError as e:
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False, indent=2))
        sys.exit(1)


if __name__ == "__main__":
    main()
