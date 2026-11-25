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
        parsed = json.loads(out)
        if isinstance(parsed, dict) and parsed.get("ok") is False:
            parsed["agent_note"] = ("Error de la herramienta: no edites el codigo del plugin. "
                                    "Corrige tus parametros y reintenta una vez; si sigue "
                                    "fallando, reporta este error al usuario.")
            return json.dumps(parsed, ensure_ascii=False)
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
    inputs = _list(params.get("inputs"))
    # carpetas o varios clips 4K tardan mas que el limite de una llamada: segundo plano
    many = len(inputs) > 1 or any(os.path.isdir(i) for i in inputs)
    return _run(args, params.get("background", many))


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


def video_reframe(params, **kwargs):
    args = ["reframe", params.get("input", "")]
    for key, flag in (("output", "-o"), ("aspect", "--aspect"), ("subject", "--subject"),
                      ("method", "--method"), ("start", "--start"), ("end", "--end"),
                      ("model", "--model")):
        if params.get(key) not in (None, ""):
            args += [flag, str(params[key])]
    return _run(args + _list(params.get("args")), params.get("background", True))


def video_shotcut(params, **kwargs):
    args = ["shotcut", params.get("input", "")]
    for key, flag in (("output", "-o"), ("aspect", "--aspect"), ("title", "--title"),
                      ("title_pos", "--title-pos"), ("title_start", "--title-start"),
                      ("title_dur", "--title-dur")):
        if params.get(key) not in (None, ""):
            args += [flag, str(params[key])]
    if params.get("open", True):
        args.append("--open")
    return _run(args)


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
             "detectados y mejor tramo de cada clip. Acepta videos o carpetas. Con una "
             "carpeta o varios clips corre en segundo plano (job_id) por defecto.",
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
    (_schema("video_reframe",
             "Pasa un video horizontal a vertical (9:16, 4:5, 1:1). Por defecto modo 'flight': "
             "sigue el vuelo/camara tal como lo hizo el piloto, centrado y suave (ideal dron). "
             "Con 'subject' el modelo de vision persigue ese sujeto. Guarda <salida>.path.json "
             "para abrirlo en Shotcut con video_shotcut. Corre en segundo plano (job_id).",
             {"input": {"type": "string"}, "output": {"type": "string"},
              "aspect": {"type": "string", "enum": ["9:16", "4:5", "1:1", "3:4"]},
              "subject": {"type": "string", "description": "que seguir: 'la cascada', 'la orilla de la playa', 'el rio'"},
              "method": {"type": "string", "enum": ["auto", "flight", "vlm", "saliency", "center"]},
              "start": {"type": "number"}, "end": {"type": "number"},
              "model": {"type": "string"},
              "args": {**ARR, "description": "ej: ['--interval','0.5','--max-speed','0.5']"},
              "background": {"type": "boolean", "description": "default true"}},
             ["input"]), video_reframe),
    (_schema("video_shotcut",
             "Crea un proyecto editable de Shotcut (.mlt) a partir del .path.json de "
             "video_reframe (recorte como keyframes de Size, Position & Rotate) o de un video, "
             "con texto opcional como capa editable, y lo abre en Shotcut para ajustes manuales "
             "(el usuario puede usar el Motion Tracker de Shotcut). Si no esta instalado, "
             "devuelve como instalarlo.",
             {"input": {"type": "string", "description": "<video>.path.json o un video"},
              "output": {"type": "string"},
              "aspect": {"type": "string", "enum": ["9:16", "4:5", "1:1", "3:4", "16:9"]},
              "title": {"type": "string"},
              "title_pos": {"type": "string", "enum": ["top", "center", "bottom"]},
              "title_start": {"type": "number"}, "title_dur": {"type": "number"},
              "open": {"type": "boolean", "description": "default true"}},
             ["input"]), video_shotcut),
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
