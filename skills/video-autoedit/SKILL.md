---
name: video-autoedit
description: Convierte una carpeta de clips en un video final listo (Reels, Shorts, TikTok, YouTube) eligiendo automaticamente los mejores tramos por calidad tecnica y, opcionalmente, atractivo visual; agrega transiciones, titulo y musica, y deja reporte, timeline y EDL para DaVinci/Premiere. Usalo cuando el usuario quiera "armar un video con estos clips", procesar las tomas del dia o un viaje/tour.
version: 0.1.0
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
