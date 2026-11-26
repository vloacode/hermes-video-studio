"""vstudio install-skills - copia los skills a OpenCode, Claude Code u otros agentes.

Fuera de Hermes no existen las herramientas video_*; los skills traen la seccion
"Sin Hermes" con los comandos `vstudio` equivalentes.
"""
import argparse
import json
import shutil
import sys
from pathlib import Path

HOME = Path.home()
TARGETS = {
    "opencode": HOME / ".config" / "opencode" / "skills",
    "claude": HOME / ".claude" / "skills",
    "agents": HOME / ".agents" / "skills",
}


def source_dir():
    here = Path(__file__).resolve().parent
    for cand in (here.parent / "skills", here / "skills"):
        if cand.is_dir() and any(cand.glob("*/SKILL.md")):
            return cand
    return None


def main(argv=None):
    ap = argparse.ArgumentParser(prog="vstudio install-skills",
                                 description="Instala los skills de video en OpenCode / Claude Code")
    ap.add_argument("--target", choices=list(TARGETS) + ["all"], default="opencode",
                    help="opencode (default), claude, agents o all")
    ap.add_argument("--dest", help="carpeta de skills propia (ej. <proyecto>/.opencode/skills)")
    ap.add_argument("--remove", action="store_true", help="desinstala los skills")
    a = ap.parse_args(argv)

    src = source_dir()
    if src is None:
        print(json.dumps({"ok": False, "error": "no encuentro la carpeta skills/ del plugin; "
                          "instala desde el repo con: pip install -e <ruta del repo>"}))
        sys.exit(1)
    names = sorted(p.parent.name for p in src.glob("*/SKILL.md"))
    if a.dest:
        dests = [Path(a.dest).expanduser()]
    elif a.target == "all":
        dests = list(TARGETS.values())
    else:
        dests = [TARGETS[a.target]]

    done = []
    for d in dests:
        for n in names:
            dst = d / n
            if a.remove:
                if dst.is_dir():
                    shutil.rmtree(dst)
                    done.append(str(dst))
            else:
                d.mkdir(parents=True, exist_ok=True)
                shutil.copytree(src / n, dst, dirs_exist_ok=True)
                done.append(str(dst))
    vstudio = shutil.which("vstudio")
    out = {"ok": True, "action": "removed" if a.remove else "installed", "skills": names,
           "paths": done, "vstudio_in_path": bool(vstudio)}
    if not a.remove:
        out["next"] = "Reinicia OpenCode / Claude Code para que cargue los skills."
        if not vstudio:
            out["warning"] = "vstudio no esta en el PATH: el agente no podra ejecutar los comandos."
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
