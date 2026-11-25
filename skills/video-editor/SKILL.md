---
name: video-editor
description: Edita videos locales con FFmpeg - cortar, unir, quitar silencios, velocidad, formato 9:16/1:1, color, estabilizar, texto, subtitulos automaticos, logos, difuminar, musica con ducking, normalizar volumen, exportar para YouTube/Shorts/Reels/TikTok, comprimir, GIF y miniaturas. Usalo cuando el usuario pida editar, recortar, subtitular, adaptar o exportar un video.
version: 0.1.0
author: vloacode
license: AGPL-3.0-only
platforms: [windows, linux, macos]
tags: [video, editing, ffmpeg, captions, shorts, reels, tiktok]
---

# Video Editor

Herramientas: `video_edit` (un comando) y `video_recipe` (varios pasos en una llamada).
Sin el plugin: `python -m video_studio edit <comando> ...` o `vstudio edit ...`.
Nada modifica el original: cada paso crea un archivo nuevo y responde JSON con `ok`.

## Regla 1 - Pedidos de 2 o mas cambios: SIEMPRE `video_recipe`
Convierte TODO lo que pidio el usuario en una lista de pasos, sin omitir ninguno:
```json
{"input": "C:/videos/C0305.mp4", "output": "C:/videos/C0305_reels.mp4",
 "steps": [{"op": "trim", "start": 25, "end": 40},
           {"op": "reframe", "aspect": "9:16", "mode": "blur"},
           {"op": "text", "text": "Dulces suenos", "position": "bottom", "box": true},
           {"op": "music", "music": "C:/musica/suave.mp3", "duck": true},
           {"op": "export", "preset": "reels"}]}
```
La respuesta trae `steps_done`: comparalo con lo pedido. Si falta algo, NO digas que terminaste.

## Regla 2 - Verificar antes de responder
Despues de editar, verifica el resultado con `video_analyze` y criterios concretos
derivados del pedido (ej. "formato vertical; se lee 'Dulces suenos' abajo; dura 15 s").
Si `verdict` = NO_CUMPLE, corrige y repite (maximo 2 veces). Reporta ruta final y pasos.

## Comandos (`op` en recipe = mismo nombre; parametros = opciones sin `--`, `_` = `-`)
| Grupo | Comandos y opciones principales |
|---|---|
| Inspeccionar | `info`, `silences --db -35 --min 0.6`, `scenes`, `frames --count`, `sheet`, `loudness`, `doctor` |
| Tiempo | `trim --start --end`, `split --at 10,1:30`, `join A B -o X`, `remove-silence --db --min --pad`, `speed --factor`, `reverse`, `loop --times` |
| Imagen | `reframe --aspect 9:16/1:1/4:5/16:9/4:3 --mode blur/crop/pad`, `resize --width`, `crop --w --h --x --y`, `color --brightness --contrast --saturation --gamma --sharpen`, `denoise --strength`, `stabilize` |
| Encima | `text --text --position --size --color --box --start --end`, `captions --language es --max-words 3 --burn`, `subtitles --subs x.srt`, `overlay --overlay logo.png --position --scale --opacity`, `blur --w --h --x --y` |
| Audio | `extract-audio --format`, `replace-audio --audio`, `music --music --volume 0.15 --duck`, `normalize --lufs -14`, `volume --factor`, `fade --fade-in --fade-out` |
| Exportar | `export --preset youtube/youtube-4k/shorts/reels/tiktok/instagram-feed/square/whatsapp --mode`, `check --preset`, `compress --mb`, `gif`, `thumbnail --at` |

Tiempos: `12.5`, `1:02` o `00:01:02.5`. Posiciones: top-left, top, top-right, center,
bottom-left, bottom, bottom-right.

## Recetas utiles
- Hablado -> Reel: remove-silence, captions (burn, max_words 3), music (duck), export reels.
- Marca: overlay logo top-right scale 0.12 opacity 0.85.
- WhatsApp: compress mb 15.

## Notas
- `captions` necesita `faster-whisper`; corre en CPU para no usar VRAM.
- GPU para codificar: variable `VEDIT_ENCODER=h264_qsv` (Intel) o `h264_nvenc` (NVIDIA).
- Si `ok` es false, lee `error`, corrige y reintenta una vez; si vuelve a fallar, informa.
