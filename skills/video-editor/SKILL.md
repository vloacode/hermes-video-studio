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

## Antes de editar: preguntar (brief)
Si el usuario no lo dijo, pregunta en UN solo mensaje (con opciones por defecto):
1. Formato: vertical 9:16 (Reels/TikTok/Shorts), horizontal 16:9 (YouTube) o cuadrado.
2. Tramo/duracion: que parte del video o cuantos segundos.
3. Sujeto principal a mantener en cuadro (ej. "la cascada", "el bebe", "el rio").
4. Texto: que dice, donde va (arriba, centro, abajo), desde que segundo y cuanto dura.
5. Musica (archivo) y si se baja cuando hay voz.
Si no responde algo, usa: 9:16, el mejor tramo de 15 s, sujeto = lo mas llamativo,
texto abajo los primeros 4 s, sin musica. Di que valores asumiste.

## Horizontal -> vertical: `video_reframe` + Shotcut
- Por defecto (`method` flight) respeta el vuelo/camara del piloto: centrado y suave. Es
  lo correcto para dron y camara en mano. NO persigas rios, caminos ni orillas.
- Solo si el usuario pide seguir algo concreto que se mueve (una persona, un bote), pasa
  `subject` y el modelo de vision lo persigue.
Flujo:
1. `video_reframe` (tramo con `start`/`end`) -> espera el job -> devuelve `path_file`.
2. Pregunta si quiere ajustar el encuadre a mano. Si si: `video_shotcut` con ese
   `path_file` (y el texto del brief): abre Shotcut con el recorte como keyframes y el
   texto como capa; el usuario ajusta o usa el Motion Tracker de Shotcut y exporta.
3. Si no: `video_recipe` sobre el video vertical para texto, musica y export.

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

## Trabajos largos (4K, videos de mas de 1 minuto, autoedit)
Usa `background: true` (en carpetas o varios clips y en autoedit ya es el default): la herramienta responde al instante con `job_id` y el trabajo
sigue aunque se corte la conexion del agente. Luego consulta `video_job_status` cada
30-60 s hasta `done`/`failed`. Si el estado es `running`, NO vuelvas a lanzar el trabajo.
Para detenerlo: `video_job_cancel`. Si se corto la sesion, `video_job_status` sin id
lista los ultimos trabajos para retomar.

## Si una herramienta falla
- NO modifiques el codigo del plugin (`video_studio/`, `__init__.py`, skills) ni crees
  scripts propios para reemplazarlo. Tus cambios se pierden al actualizar y pueden romperlo.
- Lee el `error` del JSON. Si es un parametro tuyo mal puesto, corrigelo y reintenta UNA vez.
- Si vuelve a fallar, deten el trabajo y reporta al usuario: la herramienta, los
  parametros que usaste y el error exacto, para que lo arreglen en el repositorio.
