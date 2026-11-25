"""Plugin de Hermes Agent: hermes-video-studio (AGPL-3.0).

Registra 5 herramientas que llaman al paquete `video_studio` en un subproceso (asi un
render largo o un error de FFmpeg nunca tumba al agente) y empaqueta los skills de
`skills/`. Cada herramienta devuelve SIEMPRE un JSON en texto.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TIMEOUT = int(os.environ.get("VIDEO_STUDIO_TIMEOUT", "3600"))


def _run(args, background=False):
    if background:
        args = ["job-start", *args]
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    try:
        r = subprocess.run([sys.executable, "-m", "video_studio", *args], cwd=str(ROOT),
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=TIMEOUT, env=env)
    except subprocess.TimeoutExpired:
        return json.dumps({"ok": False, "error": f"Tiempo agotado ({TIMEOUT}s)"})
    except Exception as e:  # nunca lanzar excepciones al agente
        return json.dumps({"ok": False, "error": str(e)})
    out = (r.stdout or "").strip()
    try:
        json.loads(out)
        return out
    except ValueError:
        return json.dumps({"ok": r.returncode == 0, "output": out[-6000:],
                           "stderr": (r.stderr or "")[-3000:]}, ensure_ascii=False)


def _list(v):
    if v is None:
        return []
    return [str(x) for x in (v if isinstance(v, list) else [v])]


# ---------------------------------------------------------------- handlers

def video_analyze(params, **kwargs):
    args = ["analyze", *_list(params.get("videos")), "--json"]
    if params.get("question"):
        args += ["--question", params["question"]]
    if params.get("criteria"):
        args += ["--criteria", params["criteria"]]
    if params.get("quality"):
        args.append("--quality")
    if params.get("model"):
        args += ["--model", params["model"]]
    return _run(args + _list(params.get("extra_args")), params.get("background", False))


def video_quality(params, **kwargs):
    args = ["quality", *_list(params.get("inputs")), "--json"]
    if params.get("window"):
        args += ["--window", str(params["window"])]
    return _run(args, params.get("background", False))


def video_edit(params, **kwargs):
    args = ["edit", params.get("command", "")]
    if params.get("input"):
        args.append(params["input"])
    if params.get("output"):
        args += ["-o", params["output"]]
    return _run(args + _list(params.get("args")), params.get("background", False))


def video_recipe(params, **kwargs):
    steps = params.get("steps")
    args = ["edit", "recipe", params.get("input", ""), "--steps",
            steps if isinstance(steps, str) else json.dumps(steps, ensure_ascii=False)]
    if params.get("output"):
        args += ["-o", params["output"]]
    return _run(args, params.get("background", False))


def video_autoedit(params, **kwargs):
    return _run(["autoedit", *_list(params.get("inputs")), *_list(params.get("args"))],
                params.get("background", True))


def video_job_status(params, **kwargs):
    jid = params.get("job_id")
    return _run(["job-status", jid] if jid else ["jobs"])


def video_job_cancel(params, **kwargs):
    return _run(["job-cancel", params.get("job_id", "")])


# ---------------------------------------------------------------- schemas

def _schema(name, desc, props, required):
    return {"name": name, "description": desc,
            "parameters": {"type": "object", "properties": props, "required": required}}


ARR = {"type": "array", "items": {"type": "string"}}

SCHEMAS = [
    (_schema("video_analyze",
             "Analiza/describe/evalua uno o mas videos locales con el modelo de vision del "
             "router local. Con 'criteria' devuelve verdict CUMPLE/NO_CUMPLE y "
             "suggested_prompt; con 2+ videos devuelve ranking y best.",
             {"videos": {**ARR, "description": "rutas de video"},
              "question": {"type": "string", "description": "pregunta del usuario"},
              "criteria": {"type": "string", "description": "checklist separado por ';'"},
              "quality": {"type": "boolean", "description": "incluir metricas tecnicas"},
              "model": {"type": "string", "description": "modelo con vision (default gemma4-26b)"},
              "extra_args": {**ARR, "description": "ej: ['--focus','--start','30']"},
              "background": {"type": "boolean", "description": "true = correr en segundo plano y devolver job_id al instante (usa para videos largos/4K)"}},
             ["videos"]), video_analyze),
    (_schema("video_quality",
             "Puntaje tecnico 0-100 (nitidez, exposicion, contraste, estabilidad), problemas "
             "detectados y mejor tramo de cada clip. Acepta videos o carpetas.",
             {"inputs": {**ARR, "description": "videos o carpetas"},
              "window": {"type": "number", "description": "segundos del mejor tramo"},
              "background": {"type": "boolean", "description": "true = correr en segundo plano y devolver job_id al instante (usa para videos largos/4K)"}},
             ["inputs"]), video_quality),
    (_schema("video_edit",
             "Un comando del editor: info, silences, scenes, frames, sheet, loudness, trim, "
             "split, join, remove-silence, speed, reverse, loop, reframe, resize, crop, color, "
             "denoise, stabilize, text, captions, subtitles, overlay, blur, extract-audio, "
             "replace-audio, music, normalize, volume, fade, export, check, compress, gif, "
             "thumbnail, doctor. Nunca modifica el original. Para 2+ pasos usa video_recipe.",
             {"command": {"type": "string"}, "input": {"type": "string"},
              "output": {"type": "string"},
              "args": {**ARR, "description": "ej: ['--start','5','--end','20']"},
              "background": {"type": "boolean", "description": "true = correr en segundo plano y devolver job_id al instante (usa para videos largos/4K)"}},
             ["command"]), video_edit),
    (_schema("video_recipe",
             "Varios pasos de edicion en UNA llamada, en orden; se detiene en el primer error "
             "y devuelve steps_done. Ej steps: [{\"op\":\"trim\",\"start\":25,\"end\":40},"
             "{\"op\":\"reframe\",\"aspect\":\"9:16\",\"mode\":\"blur\"},"
             "{\"op\":\"text\",\"text\":\"Hola\",\"box\":true},{\"op\":\"export\",\"preset\":\"reels\"}]",
             {"input": {"type": "string"}, "output": {"type": "string"},
              "steps": {"type": "array", "items": {"type": "object"}},
              "background": {"type": "boolean", "description": "true = correr en segundo plano y devolver job_id al instante (usa para videos largos/4K)"}},
             ["input", "steps"]), video_recipe),
    (_schema("video_autoedit",
             "Carpeta de clips -> video final: mide calidad, elige los mejores tramos, quita "
             "tomas repetidas, une con transiciones, titulo y musica, exporta y deja reporte.md, "
             "timeline.json y timeline.edl. Puede tardar varios minutos.",
             {"inputs": {**ARR, "description": "carpeta(s) o videos"},
              "args": {**ARR, "description": "ej: ['--preset','reels','--target','60',"
                                                 "'--music','m.mp3','--duck','--vision']"},
              "background": {"type": "boolean", "description": "default true: devuelve job_id al instante; consulta con video_job_status"}},
             ["inputs"]), video_autoedit),
    (_schema("video_job_status",
             "Estado de un trabajo en segundo plano (running/done/failed/cancelled) y su "
             "resultado al terminar. Sin job_id lista los ultimos trabajos. Consulta cada "
             "30-60 s; no repitas el trabajo si sigue en running.",
             {"job_id": {"type": "string"}}, []), video_job_status),
    (_schema("video_job_cancel", "Cancela un trabajo en segundo plano y sus procesos FFmpeg.",
             {"job_id": {"type": "string"}}, ["job_id"]), video_job_cancel),
]


def register(ctx):
    for schema, handler in SCHEMAS:
        ctx.register_tool(name=schema["name"], toolset="video_studio", schema=schema,
                          handler=handler)
    skills_dir = ROOT / "skills"
    if skills_dir.is_dir():
        for child in sorted(skills_dir.iterdir()):
            skill_md = child / "SKILL.md"
            if child.is_dir() and skill_md.exists():
                ctx.register_skill(child.name, skill_md)
