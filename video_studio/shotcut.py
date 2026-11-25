"""video_studio.shotcut - exporta el trabajo de la IA como proyecto de Shotcut (.mlt).

El recorrido del reencuadre (<video>.path.json) se convierte en keyframes del filtro
"Size, Position & Rotate"; el texto va como filtro "Text: Simple" con su tiempo. Asi
abres el proyecto en Shotcut y ajustas a mano (o usas su Motion Tracker) lo que quieras.

  vstudio shotcut video.path.json [--title "Texto" --title-start 0 --title-dur 4] [--open]
  vstudio shotcut video.mp4 --aspect 9:16 [--open]       (sin reencuadre: recorte centrado)
"""
import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
from fractions import Fraction
from xml.sax.saxutils import escape

from .edit import VeditError, probe

PROFILES = {"9:16": (1080, 1920), "4:5": (1080, 1350), "1:1": (1080, 1080),
            "3:4": (1080, 1440), "16:9": (1920, 1080)}
POSITIONS = {"top": ("center", "top"), "center": ("center", "middle"),
             "bottom": ("center", "bottom")}


def tstr(sec):
    sec = max(0.0, sec)
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return f"{int(h):02d}:{int(m):02d}:{s:06.3f}"


def src_fps(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                        "stream=r_frame_rate", "-of", "csv=p=0", path], capture_output=True,
                       text=True)
    try:
        return Fraction(r.stdout.strip() or "30/1")
    except (ValueError, ZeroDivisionError):
        return Fraction(30, 1)


def find_shotcut():
    cands = [shutil.which("shotcut")]
    for base in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                 os.environ.get("LOCALAPPDATA", "")):
        cands += glob.glob(os.path.join(base, "Shotcut", "shotcut.exe"))
        cands += glob.glob(os.path.join(base, "Programs", "Shotcut", "shotcut.exe"))
    cands += ["/Applications/Shotcut.app/Contents/MacOS/Shotcut"]
    return next((c for c in cands if c and os.path.exists(c)), None)


def prop(name, value):
    return f'    <property name="{name}">{escape(str(value))}</property>\n'


def build(a):
    if a.input.lower().endswith(".json"):
        with open(a.input, encoding="utf-8") as fh:
            pj = json.load(fh)
        src, start, dur = pj["source"], pj["start"], pj["duration"]
        aspect, xs, afps = pj.get("aspect", "9:16"), pj["x_center"], pj["fps"]
    else:
        info = probe(a.input)
        src, start = os.path.abspath(a.input), a.start or 0.0
        dur = (a.end - start) if a.end else info["duration"] - start
        aspect, xs, afps = a.aspect, [0.5], 1.0
    if not os.path.exists(src):
        raise VeditError(f"No encuentro el video fuente: {src}")
    info = probe(src)
    W, H = info["width"], info["height"]
    PW, PH = PROFILES[aspect]
    fps = src_fps(src)

    # escala para que el video llene la altura del proyecto
    scale = max(PH / H, PW / W)
    vw, vh = W * scale, H * scale
    keys = []
    step = max(1, int(round(afps * a.key_every)))
    idx = list(range(0, len(xs), step))
    if idx[-1] != len(xs) - 1:
        idx.append(len(xs) - 1)
    for i in idx:
        x = PW / 2 - xs[i] * vw
        x = min(0.0, max(PW - vw, x))
        keys.append(f"{tstr(i / afps)}~={x:.1f} {(PH - vh) / 2:.1f} {vw:.1f} {vh:.1f} 1")
    rect = ";".join(keys)

    name = os.path.splitext(os.path.basename(src))[0]
    out_fr = tstr(dur - float(1 / fps))
    filters = []
    filters.append(
        '  <filter id="filter0">\n' + prop("background", "color:#00000000")
        + prop("mlt_service", "affine") + prop("shotcut:filter", "affineSizePosition")
        + prop("transition.fill", 1) + prop("transition.distort", 0)
        + prop("transition.rect", rect) + prop("transition.valign", "middle")
        + prop("transition.halign", "center") + prop("transition.threads", 0)
        + prop("shotcut:animIn", "00:00:00.000") + prop("shotcut:animOut", "00:00:00.000")
        + "  </filter>\n")
    if a.title:
        ha, va = POSITIONS[a.title_pos]
        t_in, t_out = a.title_start, a.title_start + a.title_dur
        size = int(PH * 0.045)
        filters.append(
            f'  <filter id="filter1" in="{tstr(t_in)}" out="{tstr(t_out)}">\n'
            + prop("mlt_service", "dynamictext") + prop("shotcut:filter", "dynamicText")
            + prop("argument", a.title) + prop("geometry", f"0 {int(PH * 0.06)} {PW} "
                                                          f"{int(PH * 0.88)} 1")
            + prop("family", "Arial") + prop("size", size) + prop("weight", 700)
            + prop("style", "normal") + prop("fgcolour", "#ffffffff")
            + prop("bgcolour", "#00000000") + prop("olcolour", "#cc000000")
            + prop("outline", 3) + prop("pad", 0) + prop("halign", ha) + prop("valign", va)
            + "  </filter>\n")

    W_, H_ = PW, PH
    xml = f'''<?xml version="1.0" standalone="no"?>
<mlt LC_NUMERIC="C" version="7.22.0" title="hermes-video-studio" producer="main_bin">
  <profile description="{aspect} {W_}x{H_}" width="{W_}" height="{H_}" progressive="1"
    sample_aspect_num="1" sample_aspect_den="1" display_aspect_num="{W_}" display_aspect_den="{H_}"
    frame_rate_num="{fps.numerator}" frame_rate_den="{fps.denominator}" colorspace="709"/>
  <playlist id="main_bin">
    <property name="xml_retain">1</property>
  </playlist>
  <producer id="black" in="00:00:00.000" out="{out_fr}">
    <property name="length">{tstr(dur)}</property>
    <property name="eof">pause</property>
    <property name="resource">0</property>
    <property name="aspect_ratio">1</property>
    <property name="mlt_service">color</property>
    <property name="mlt_image_format">rgba</property>
    <property name="set.test_audio">0</property>
  </producer>
  <playlist id="background">
    <entry producer="black" in="00:00:00.000" out="{out_fr}"/>
  </playlist>
  <producer id="producer0" in="{tstr(start)}" out="{tstr(start + dur - float(1 / fps))}">
{prop("length", tstr(info["duration"]))}{prop("eof", "pause")}{prop("resource", src)}{prop("mlt_service", "avformat-novalidate")}{prop("shotcut:caption", name)}{prop("seekable", 1)}{"".join(filters)}  </producer>
  <playlist id="playlist0">
    <property name="shotcut:video">1</property>
    <property name="shotcut:name">V1</property>
    <entry producer="producer0" in="{tstr(start)}" out="{tstr(start + dur - float(1 / fps))}"/>
  </playlist>
  <tractor id="tractor0" title="hermes-video-studio" in="00:00:00.000" out="{out_fr}">
    <property name="shotcut">1</property>
    <track producer="background"/>
    <track producer="playlist0"/>
    <transition id="transition0">
      <property name="a_track">0</property>
      <property name="b_track">1</property>
      <property name="mlt_service">mix</property>
      <property name="always_active">1</property>
      <property name="sum">1</property>
    </transition>
    <transition id="transition1">
      <property name="a_track">0</property>
      <property name="b_track">1</property>
      <property name="version">0.1</property>
      <property name="mlt_service">frei0r.cairoblend</property>
      <property name="threads">0</property>
      <property name="disable">1</property>
    </transition>
  </tractor>
</mlt>
'''
    out = os.path.abspath(a.output or os.path.join(os.path.dirname(src), f"{name}_{aspect.replace(':', 'x')}.mlt"))
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(xml)
    res = {"ok": True, "project": out, "aspect": aspect, "keyframes": len(keys),
           "duration": round(dur, 2), "title": bool(a.title)}
    if a.open:
        exe = find_shotcut()
        if exe:
            subprocess.Popen([exe, out])
            res["opened_with"] = exe
        else:
            res["opened_with"] = None
            res["install_hint"] = ("Shotcut no esta instalado. Instalalo gratis desde "
                                   "https://shotcut.org o con: winget install Meltytech.Shotcut")
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(prog="vstudio shotcut",
                                 description="Exporta a un proyecto editable de Shotcut")
    ap.add_argument("input", help="<video>.path.json del reencuadre, o un video")
    ap.add_argument("-o", "--output", help="archivo .mlt")
    ap.add_argument("--aspect", choices=list(PROFILES), default="9:16")
    ap.add_argument("--start", type=float, default=0.0)
    ap.add_argument("--end", type=float)
    ap.add_argument("--key-every", type=float, default=1.0, help="segundos entre keyframes")
    ap.add_argument("--title")
    ap.add_argument("--title-pos", choices=list(POSITIONS), default="bottom")
    ap.add_argument("--title-start", type=float, default=0.0)
    ap.add_argument("--title-dur", type=float, default=4.0)
    ap.add_argument("--open", action="store_true", help="abrir en Shotcut al terminar")
    a = ap.parse_args(argv)
    try:
        res = build(a)
    except (VeditError, RuntimeError, OSError, KeyError) as e:
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False, indent=2))
        sys.exit(1)
    print(json.dumps(res, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
