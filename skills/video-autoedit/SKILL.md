---
name: video-autoedit
description: Convierte una carpeta de clips en un video final listo (Reels, Shorts, TikTok, YouTube) eligiendo automaticamente los mejores tramos por calidad tecnica y, opcionalmente, atractivo visual; agrega transiciones, titulo y musica, y deja reporte, timeline y EDL para DaVinci/Premiere. Usalo cuando el usuario quiera "armar un video con estos clips", procesar las tomas del dia o un viaje/tour.
version: 0.2.0
author: vloacode
license: AGPL-3.0-only
platforms: [windows, linux, macos]
tags: [video, autoedit, reels, highlights, travel, ffmpeg]
---

# Video Autoedit

Herramienta: `video_autoedit` (`inputs` = carpeta o videos, `args` = opciones).
Sin el plugin: `vstudio autoedit <carpeta> [opciones]`.

## Que hace
1. Puntua cada clip (nitidez, exposicion, contraste, estabilidad) y encuentra su mejor tramo.
2. Con `--vision`, el modelo de vision puntua 1-10 el atractivo de cada toma (60% calidad,
   40% vision). Usa `--topic "tour en kayak"` para darle contexto.
3. Quita tomas casi iguales, elige hasta la duracion objetivo y las ordena por nombre
   (cronologico en camaras).
4. Recorta, adapta el formato, une con transiciones, titulo, musica y volumen a -14 LUFS.
5. Deja en la carpeta de salida: `<nombre>_<preset>.mp4`, `reporte.md`, `timeline.json`,
   `timeline.edl` (importable en DaVinci/Premiere) y `quality.json` (cache).

## Opciones
| Opcion | Default | Uso |
|---|---|---|
| `--preset` | reels | youtube, youtube-4k, shorts, reels, tiktok, instagram-feed, square, whatsapp |
| `--target / --max-target` | 60 / 80 | duracion deseada en segundos |
| `--clip-max / --clip-min` | 6 / 2 | duracion por toma |
| `--min-label` | BUENO | nivel minimo de calidad (EXCELENTE, BUENO, ACEPTABLE, DESCARTAR) |
| `--mode` | crop | blur/pad si no se quiere recortar al cambiar formato |
| `--transition` / `--xfade` | 0.35 / fade | 0 = cortes secos; fadeblack, wipeleft, smoothleft... |
| `--title` | - | texto al inicio |
| `--music` `--music-volume` `--duck` | - / 0.25 | musica de fondo; duck baja la musica con voz |
| `--mute-clips` | - | solo musica |
| `--vision --topic` | - | puntuar atractivo con el modelo de vision |
| `--order` | name | name, time o score |
| `--dry-run` | - | solo analiza y elige (rapido), sin renderizar |
| `--inbox X --outbox Y --mark-done` | - | procesa cada subcarpeta pendiente de X y la renombra " - DONE" |

## Reglas
1. Si el usuario no da duracion ni formato, pregunta solo si importa; si no, usa reels 60-80 s.
2. Para muchas tomas o 4K, avisa que puede tardar varios minutos.
3. Al terminar resume con el reporte: clips usados, duracion y ruta. Ofrece ajustar
   (otra musica, mas corto, otro formato) re-ejecutando: el analisis queda en cache.
4. Si responde "Ningun clip alcanza el nivel minimo", ofrece `--min-label ACEPTABLE`.

## Trabajos largos (4K, videos de mas de 1 minuto, autoedit)
Usa `background: true` (en carpetas o varios clips y en autoedit ya es el default): la herramienta responde al instante con `job_id` y el trabajo
sigue aunque se corte la conexion del agente. Luego consulta `video_job_status` cada
30-60 s hasta `done`/`failed`. Si el estado es `running`, NO vuelvas a lanzar el trabajo.
Para detenerlo: `video_job_cancel`. Si se corto la sesion, `video_job_status` sin id
lista los ultimos trabajos para retomar.

## Sin Hermes (OpenCode, Claude Code, terminal)
Si no tienes las herramientas `video_*`, usa el comando `vstudio` en la terminal. Todos
responden JSON; los parametros son los mismos con `--` y `_` -> `-`.

| Herramienta de Hermes | Comando |
|---|---|
| `video_analyze` | `vstudio analyze VIDEO --question "..." --json` |
| `video_quality` | `vstudio quality VIDEOS_O_CARPETA --json` |
| `video_edit` | `vstudio edit <comando> VIDEO -o SALIDA [...]` (`vstudio edit -h`) |
| `video_recipe` | `vstudio edit recipe VIDEO -o SALIDA --steps pasos.json` |
| `video_reframe` | `vstudio reframe VIDEO --aspect 9:16 --start S --end E` |
| `video_shotcut` | `vstudio shotcut VIDEO.path.json --title "..." --open` |
| `video_autoedit` | `vstudio autoedit CARPETA --preset reels` |
| `video_job_status` | `vstudio job-status ID` (sin ID: `vstudio jobs`) |
| `video_job_cancel` | `vstudio job-cancel ID` |

- Recipe: escribe los pasos en un archivo `pasos.json` (lista JSON) y pasalo con
  `--steps pasos.json`. No pongas el JSON en la linea de comandos: las comillas se rompen.
- La terminal del agente corta los comandos largos. `autoedit`, `reframe`, `quality` de
  carpetas o varios clips, y cualquier video 4K o de mas de 1 minuto, SIEMPRE con
  `vstudio job-start <sub> ...`: responde al instante con `job_id`. Luego
  `vstudio job-status ID` cada 30-60 s hasta `done`/`failed`. Nunca relances un job `running`.
- Rutas de Windows con espacios: siempre entre comillas.

## Si una herramienta falla
- NO modifiques el codigo del plugin (`video_studio/`, `__init__.py`, skills) ni crees
  scripts propios para reemplazarlo. Tus cambios se pierden al actualizar y pueden romperlo.
- Lee el `error` del JSON. Si es un parametro tuyo mal puesto, corrigelo y reintenta UNA vez.
- Si vuelve a fallar, deten el trabajo y reporta al usuario: la herramienta, los
  parametros que usaste y el error exacto, para que lo arreglen en el repositorio.
