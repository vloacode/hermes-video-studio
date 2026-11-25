"""video_studio.jobs - trabajos en segundo plano.

Los renders y analisis largos (4K, autoedit) pueden tardar minutos. Si el agente espera
dentro de la llamada y su conexion se corta, no sabe si el trabajo termino. Con un job:
  1. job-start lanza el comando en un proceso INDEPENDIENTE y responde al instante con un id.
  2. job-status dice running / done / failed / cancelled y, al terminar, el JSON del resultado.
  3. job-cancel lo detiene (y a sus FFmpeg hijos).
El trabajo sigue aunque Hermes, el gateway o la terminal se cierren.
"""
import json
import os
import signal
import subprocess
import sys
import time
import uuid

JOBS = os.environ.get("VIDEO_STUDIO_JOBS") or os.path.join(
    os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/.cache"), "video-studio", "jobs")


def _dir(job_id):
    return os.path.join(JOBS, job_id)


def _write(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _read(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _alive(pid):
    if not pid:
        return False
    if os.name == "nt":
        r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True,
                           text=True, errors="replace")
        return str(pid) in r.stdout
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def start(argv):
    """Lanza `vstudio <argv>` desacoplado y devuelve el id."""
    job_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    d = _dir(job_id)
    os.makedirs(d, exist_ok=True)
    log = open(os.path.join(d, "log.txt"), "w", encoding="utf-8")
    cmd = [sys.executable, "-m", "video_studio", "job-run", job_id, "--", *argv]
    kw = {"stdout": log, "stderr": subprocess.STDOUT, "stdin": subprocess.DEVNULL,
          "cwd": os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
          "env": dict(os.environ, PYTHONIOENCODING="utf-8")}
    if os.name == "nt":
        kw["creationflags"] = (subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000008  # DETACHED
                               | 0x08000000)  # CREATE_NO_WINDOW
    else:
        kw["start_new_session"] = True
    p = subprocess.Popen(cmd, **kw)
    _write(os.path.join(d, "job.json"), {"id": job_id, "argv": argv, "pid": p.pid,
                                         "started": time.time(), "status": "running"})
    return {"ok": True, "job_id": job_id, "status": "running",
            "hint": "Consulta con video_job_status / vstudio job-status " + job_id}


def run(job_id, argv):
    """Proceso hijo: ejecuta el comando y guarda result.json."""
    d = _dir(job_id)
    meta = _read(os.path.join(d, "job.json")) or {"id": job_id}
    cmd = [sys.executable, "-m", "video_studio", *argv]
    kw = {"capture_output": True, "text": True, "encoding": "utf-8", "errors": "replace",
          "env": dict(os.environ, PYTHONIOENCODING="utf-8")}
    try:
        r = subprocess.run(cmd, **kw)
        out = (r.stdout or "").strip()
        try:
            result = json.loads(out)
        except ValueError:
            result = {"ok": r.returncode == 0, "output": out[-6000:]}
        if r.stderr:
            with open(os.path.join(d, "log.txt"), "a", encoding="utf-8") as fh:
                fh.write(r.stderr[-20000:])
        status = "done" if result.get("ok", r.returncode == 0) else "failed"
    except Exception as e:  # noqa: BLE001
        result, status = {"ok": False, "error": str(e)}, "failed"
    if (_read(os.path.join(d, "job.json")) or {}).get("status") == "cancelled":
        return
    _write(os.path.join(d, "result.json"), result)
    meta.update(status=status, finished=time.time())
    _write(os.path.join(d, "job.json"), meta)


def status(job_id):
    d = _dir(job_id)
    meta = _read(os.path.join(d, "job.json"))
    if not meta:
        return {"ok": False, "error": f"No existe el job {job_id}"}
    res = {"ok": True, "job_id": job_id, "status": meta.get("status"),
           "elapsed_s": round((meta.get("finished") or time.time()) - meta["started"], 1)}
    if meta.get("status") == "running" and not _alive(meta.get("pid")):
        res["status"] = "failed"
        res["error"] = "El proceso termino sin guardar resultado (revisa el log)."
    result = _read(os.path.join(d, "result.json"))
    if result is not None:
        res["result"] = result
    try:
        with open(os.path.join(d, "log.txt"), encoding="utf-8", errors="replace") as fh:
            lines = [ln.rstrip() for ln in fh.readlines()[-6:] if ln.strip()]
        res["last_log"] = lines
    except OSError:
        pass
    return res


def cancel(job_id):
    d = _dir(job_id)
    meta = _read(os.path.join(d, "job.json"))
    if not meta:
        return {"ok": False, "error": f"No existe el job {job_id}"}
    pid = meta.get("pid")
    if meta.get("status") == "running" and _alive(pid):
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
        else:
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            except OSError:
                pass
    meta.update(status="cancelled", finished=time.time())
    _write(os.path.join(d, "job.json"), meta)
    return {"ok": True, "job_id": job_id, "status": "cancelled"}


def list_jobs(limit=10):
    if not os.path.isdir(JOBS):
        return {"ok": True, "jobs": []}
    ids = sorted(os.listdir(JOBS), reverse=True)[:limit]
    return {"ok": True, "jobs": [{k: v for k, v in status(i).items()
                                  if k in ("job_id", "status", "elapsed_s")} for i in ids]}
