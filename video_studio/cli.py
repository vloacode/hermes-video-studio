"""vstudio - punto de entrada unico: vstudio <analyze|quality|edit|autoedit|doctor> ..."""
import sys

USAGE = """vstudio - hermes-video-studio

  vstudio analyze   VIDEO [VIDEO...] [--question ..] [--criteria ..] [--quality] [--json]
  vstudio quality   VIDEOS|CARPETA [--window 8] [--json] [--report r.md]
  vstudio edit      <comando> VIDEO [...]      (vstudio edit -h  para los 35 comandos)
  vstudio autoedit  CARPETA [--preset reels] [--target 60] [--music m.mp3] [--vision]
  vstudio doctor

Ayuda de cada uno: vstudio <sub> -h
"""


def _utf8_stdio():
    # En Windows, al redirigir la salida Python usa cp1252 y rompe la n/acentos del JSON.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def main(argv=None):
    _utf8_stdio()
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(USAGE)
        return
    sub, rest = argv[0], argv[1:]
    if sub == "analyze":
        from .analyze import main as m
    elif sub == "quality":
        from .quality import main as m
    elif sub == "edit":
        from .edit import main as m
    elif sub == "autoedit":
        from .autoedit import main as m
    elif sub == "doctor":
        from .edit import main as m
        rest = ["doctor"]
    elif sub == "--version":
        from . import __version__
        print(__version__)
        return
    else:
        print(USAGE)
        sys.exit(2)
    m(rest)


if __name__ == "__main__":
    main()
