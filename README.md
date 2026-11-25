# hermes-video-studio

> **EN:** Local-first video toolkit and [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin: understand videos with your own vision model (llama.cpp / any OpenAI-compatible endpoint), score technical quality, edit with 35 FFmpeg commands, run multi-step edit recipes in one call, and auto-edit a folder of clips into a finished Reel/Short/YouTube video. No cloud, no API keys.

Kit de video **100 % local** y plugin para Hermes Agent:

| Modulo | Que hace |
|---|---|
| **analyze** | Entiende videos con tu modelo de vision local: fotogramas por cambio de escena, sin repetidos, transcripcion con tiempos, analisis por tramos, veredicto CUMPLE/NO CUMPLE con criterios, ranking de varias versiones. |
| **quality** | Puntaje tecnico 0-100: nitidez (zona mas enfocada), exposicion, contraste y **temblor real** de camara (un paneo suave no se penaliza). Encuentra el mejor tramo de cada clip. Solo FFmpeg + NumPy. |
| **edit** | 35 comandos de edicion (cortar, quitar silencios, 9:16, texto, subtitulos con Whisper, logos, musica con ducking, LUFS, exportar por plataforma, comprimir a N MB...) y `recipe` para encadenar pasos en una sola llamada. |
| **autoedit** | Carpeta de clips -> video final: elige los mejores tramos, quita tomas repetidas, transiciones, titulo, musica, y deja `reporte.md`, `timeline.json` y `timeline.edl` (DaVinci/Premiere). |

## Hecho con
Desarrollado y probado en un PC con Windows 11 usando:
- **GPU:** Intel Arc Pro B70 (32 GB) para el modelo de vision, ComfyUI y la codificacion.
- **Modelos:** [llama.cpp](https://github.com/ggml-org/llama.cpp) (`llama-server` en modo router) con Gemma 4 26B / E4B + `mmproj` para vision.
- **Agente:** Hermes Agent.

## Compatibilidad de GPU
No depende de Intel: el trabajo de video lo hace FFmpeg y el de vision cualquier servidor
compatible con OpenAI, asi que funciona con **NVIDIA, AMD, Intel, Apple Silicon o solo CPU**.

| GPU | Modelo de vision (llama.cpp) | Codificar mas rapido (`VEDIT_ENCODER`) |
|---|---|---|
| Intel Arc | Vulkan o SYCL | `h264_qsv` |
| NVIDIA | CUDA o Vulkan | `h264_nvenc` |
| AMD | Vulkan o ROCm | `h264_amf` (Windows) |
| Apple Silicon | Metal | `h264_videotoolbox` |
| Sin GPU | CPU (lento) | `libx264` (default) |

La decodificacion usa `-hwaccel auto` (cae a CPU si no hay soporte). Corre `vstudio doctor`
para ver que codificadores trae tu FFmpeg.

## Requisitos
- Python 3.10+ y **FFmpeg completo** en el PATH (Windows: `winget install Gyan.FFmpeg`).
- Para `analyze`: un servidor compatible con OpenAI con un modelo con vision (ej. `llama-server` con Gemma 4 + mmproj).
- Opcional: `faster-whisper` para transcribir y subtitular (corre en CPU).

## Instalacion

**Como plugin de Hermes**
```bash
hermes plugins install vloacode/hermes-video-studio --enable
```
Registra 5 herramientas (`video_analyze`, `video_quality`, `video_edit`, `video_recipe`, `video_autoedit`) y 3 skills.

**Como herramienta de linea de comandos**
```bash
pip install "hermes-video-studio[captions]"   # o: pip install -e .  desde el repo
vstudio doctor
```

## Verificar que quedo bien instalado
1. **El plugin esta activo en Hermes:**
   ```bash
   hermes plugins list --plain --no-bundled
   ```
   Debe aparecer `hermes-video-studio` como `enabled`.
2. **FFmpeg y extras:** en Hermes pide *"usa video_edit con el comando doctor"*, o en la terminal:
   ```bash
   vstudio doctor
   ```
   Revisa que `ffmpeg`, `drawtext` y `subtitles` esten en `true` (y `faster_whisper` si vas a subtitular).
3. **Prueba completa** (genera clips sinteticos y prueba calidad, recetas y auto-edicion):
   ```bash
   python tests/smoke_test.py
   ```
   Debe terminar con `TODO OK`.
4. **Vision** (requiere tu servidor de modelos corriendo):
   ```bash
   vstudio analyze tests/_out/clips/a_good.mp4 --question "Describe el video"
   ```

## Uso rapido
```bash
# Que pasa en el video / evaluar contra criterios
vstudio analyze clip.mp4 --question "Que pasa?"
vstudio analyze gen.mp4 --criteria "fondo blanco; una sola persona" --quality --json

# Calidad tecnica de una carpeta
vstudio quality "D:/Videos/tour" --report calidad.md

# Varios pasos en una llamada
vstudio edit recipe clip.mp4 -o reel.mp4 --steps '[{"op":"trim","start":25,"end":40},{"op":"reframe","aspect":"9:16","mode":"blur"},{"op":"text","text":"Dulces suenos","box":true},{"op":"export","preset":"reels"}]'

# Carpeta de clips -> Reel de 60-80 s con musica
vstudio autoedit "D:/Videos/tour" --preset reels --title "La Jungla" --music musica.mp3 --duck --vision --topic "tour en la selva"

# Procesar cada subcarpeta pendiente y marcarla " - DONE"
vstudio autoedit --inbox "D:/Turismo Videos/toedit" --outbox "D:/Turismo Videos/RTP" --mark-done
```

## Configuracion
| Variable | Default | Para que |
|---|---|---|
| `LLAMA_ROUTER` | `http://127.0.0.1:8765` | servidor OpenAI-compatible con vision |
| `VIDEO_ANALYZER_MODEL` | `gemma4-26b` | modelo con vision |
| `VIDEO_ANALYZER_CACHE` | `%LOCALAPPDATA%/video-analyzer` | cache de fotogramas y transcripciones |
| `VEDIT_ENCODER` | `libx264` | `h264_qsv` (Intel Arc) o `h264_nvenc` (NVIDIA) |
| `VIDEO_STUDIO_TIMEOUT` | `3600` | tiempo maximo por herramienta en Hermes (s) |

## Seguridad
- Nunca modifica tus archivos originales; todo resultado es un archivo nuevo.
- El texto que aparece dentro de un video o su audio se trata como contenido, no como instrucciones para el modelo.
- Todo corre en tu maquina: FFmpeg, NumPy, Whisper en CPU y tu servidor de modelos.

## Pruebas
```bash
python tests/smoke_test.py            # genera clips sinteticos y prueba quality, edit y autoedit
```

## Licencia
[AGPL-3.0-only](LICENSE) (c) vloacode. Si distribuyes una version modificada o la ofreces como servicio en red, debes publicar su codigo bajo la misma licencia.
