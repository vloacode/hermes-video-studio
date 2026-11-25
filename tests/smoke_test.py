"""Prueba de humo: genera clips sinteticos y ejecuta quality, edit (recipe) y autoedit.

Uso: python tests/smoke_test.py   (requiere ffmpeg/ffprobe en el PATH)
"""
import json
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "tests", "_out")


def sh(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


def vs(*args):
    r = subprocess.run([sys.executable, "-m", "video_studio", *args], cwd=ROOT,
                       capture_output=True, text=True, encoding="utf-8")
    try:
        return json.loads(r.stdout)
    except ValueError:
        raise AssertionError(f"Salida no JSON:\n{r.stdout}\n{r.stderr}")


def main():
    shutil.rmtree(OUT, ignore_errors=True)
    clips = os.path.join(OUT, "clips")
    os.makedirs(clips)
    good = os.path.join(clips, "a_good.mp4")
    sh("-f", "lavfi", "-i", "testsrc2=s=1280x720:d=8:r=30", "-f", "lavfi", "-i",
       "sine=f=440:d=8", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", good)
    sh("-i", good, "-vf", "boxblur=8:2", "-c:v", "libx264", "-pix_fmt", "yuv420p",
       os.path.join(clips, "b_blurry.mp4"))
    sh("-f", "lavfi", "-i", "testsrc=s=1280x720:d=8:r=30", "-c:v", "libx264", "-pix_fmt",
       "yuv420p", os.path.join(clips, "c_other.mp4"))

    q = vs("quality", clips, "--json", "--window", "3")
    scores = {c["file"]: c for c in q["clips"]}
    assert scores["a_good.mp4"]["score"] > scores["b_blurry.mp4"]["score"], scores
    assert "borroso/desenfocado" in scores["b_blurry.mp4"]["issues"]
    print("quality OK")

    steps = [{"op": "trim", "start": 1, "end": 5}, {"op": "reframe", "aspect": "9:16",
             "mode": "blur", "height": 640}, {"op": "text", "text": "Prueba ñ", "box": True}]
    r = vs("edit", "recipe", good, "-o", os.path.join(OUT, "recipe.mp4"), "--steps",
           json.dumps(steps))
    assert r["ok"] and len(r["steps_done"]) == 3 and r["height"] == 640, r
    print("recipe OK")

    r = vs("autoedit", clips, "-o", os.path.join(OUT, "auto"), "--target", "6",
           "--max-target", "9", "--clip-max", "3", "--title", "Smoke", "--min-label",
           "ACEPTABLE")
    assert r["ok"] and r["clips_used"] >= 2 and r["height"] == 1920, r
    for f in ("report", "timeline", "edl"):
        assert os.path.exists(r[f]), f
    print("autoedit OK")
    print("TODO OK")


if __name__ == "__main__":
    main()
