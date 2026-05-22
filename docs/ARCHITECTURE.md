# Web Radio Architecture

## Overview

The solution is built from five components arranged in a pipeline. Each component has a single responsibility and communicates with the others through well-defined interfaces (local RTMP, HTTP webhooks, PCM pipe, HLS file system).

```
 OBS Studio / Mixxx
        │  RTMP (WAN or LAN)
        ▼
 ┌─────────────────────────────────────────┐
 │              nginx-rtmp                 │
 │   exec_publish    →  on_publish.sh      │
 │   exec_publish_done → on_unpublish.sh   │
 └──────────┬──────────────────────────────┘
            │ HTTP POST webhook + flag file
            ▼
 ┌─────────────────────────────────────────┐
 │          Python Supervisor              │
 │  • FFmpeg watchdog                      │
 │  • live ↔ fallback switching            │
 │  • MariaDB schedule reader              │
 │  • ID3 metadata injection (ts_inject)   │
 └──────────┬──────────────────────────────┘
            │ subprocess commands (PCM pipe or RTMP)
            ▼
 ┌─────────────────────────────────────────┐
 │           FFmpeg HLS                    │
 │  writes .ts segments + .m3u8 playlist   │
 │  to /opt/webradio/hls/                  │
 └──────────┬──────────────────────────────┘
            │ static HTTP files
            ▼
 ┌─────────────────────────────────────────┐
 │     nginx (HTTP server :80 or :443)     │
 │  serves /opt/webradio/hls/ to CDN/player│
 └─────────────────────────────────────────┘
```

The administrator interacts via an external tool (Mixxx, script, or web interface) that populates the **MariaDB** database (`webradio`) with tracks and the daily schedule. The supervisor queries this database at startup and at midnight to build the playback queue.

---

## Component Details

### nginx-rtmp

nginx is packaged or compiled with the `nginx-rtmp` module. It serves two simultaneous roles:

- **RTMP ingest** (port 1935): receives the audio stream from OBS Studio or Mixxx. Whenever a publisher connects or disconnects, nginx-rtmp executes the shell scripts `on_publish.sh` and `on_unpublish.sh` via the `exec_publish` and `exec_publish_done` directives.
- **HTTP server**: serves the `/opt/webradio/hls/` directory as static HTTP for CDN pull or directly for HLS players.


### Shell Scripts (on_publish.sh / on_unpublish.sh)

These scripts provide **dual signalling** to the supervisor:

1. They create or delete the flag file `/tmp/webradio_live.flag` — polled by the supervisor every 2 seconds, independently of the network.
2. They send an HTTP `POST` to `http://127.0.0.1:8089/on_publish` or `/on_unpublish` — the fast path (near-instant).

### Python Supervisor

The heart of the system. A single long-running process that orchestrates all other components.

**Three OBS state detection sources:**

| Source | Mechanism | Latency |
|---|---|---|
| HTTP webhook | POST from on_publish.sh | < 100 ms |
| Flag file | Polled every 2 s | ≤ 2 s |
| nginx-rtmp stat API | XML polled every 10 s | ≤ 10 s |

All three run simultaneously. Sources 2 and 3 act as safety nets in case a webhook is missed (supervisor restart at the wrong moment, local network hiccup).

**Grace delay**: 8 seconds elapse after an `on_unpublish` event before switching to fallback. This absorbs OBS micro-disconnects (scene reload, profile switch) without interrupting the HLS stream.

**FFmpeg watchdog**: every 5 seconds, the supervisor checks that the HLS FFmpeg process is still running. On an unexpected crash, it restarts it automatically in the correct mode (live or fallback).

### FFmpeg HLS

FFmpeg is driven differently depending on the active mode:

**LIVE mode**: FFmpeg reads the local RTMP stream (`rtmp://127.0.0.1/live/stream`) and transcodes it directly into AAC-LC HLS segments.

**FALLBACK mode**: the supervisor launches an `AudioPipe` — a Python thread decodes each audio file to raw PCM (`s16le 44100 Hz stereo`) via a decoder FFmpeg instance with `-re` (real-time speed), and pushes chunks onto the `stdin` of a single HLS muxer FFmpeg instance. The muxer sees a continuous PCM stream and never produces a segment number gap.

```
AudioPipe._feed()
  └── FFmpeg decoder (-re) → PCM stdout
        └── written in chunks to stdin
              └── FFmpeg HLS muxer → seg00001.ts, seg00002.ts ...
```

### SegmentWatcher + ts_inject

A dedicated thread scans the HLS directory every 500 ms. As soon as a new `.ts` segment appears and its size is stable (two identical measurements 150 ms apart), it calls the external `ts_inject` binary, which:

- reads the real PTS of the audio segment,
- patches the PMT (adds `stream_type=0x15`, `PID=0x0015`),
- builds an ID3v2.3 tag with `TIT2` (title) and `TPE1` (artist) frames,
- atomically rewrites the segment.

Compatible HLS players (HLS.js, Safari, VLC) then display the current title and artist.

### MariaDB Database

Four-table schema:

```
genre         (id_genre, nom_genre)
artiste       (id_artiste, nom_artiste)
titre         (id_titre, nom_titre, chemin, id_artiste, id_genre, duree)
programmation (id_prog, date_prog, ordre, id_titre)
```

The `programmation` table associates a date with an ordered list of tracks (`ordre`). The [import_music](https://github.com/g-maulino/webrad/blob/main/db/02_import_music.py) script is responsible for populating the titre and artistes tables from scanning audio file folder.

---

## Operating Modes

### LIVE Mode (OBS/Mixxx connected)

OBS Studio or Mixxx sends an RTMP stream to nginx-rtmp. The supervisor receives the `on_publish` webhook, cancels any ongoing grace delay, stops the fallback pipe and starts FFmpeg in RTMP→HLS relay mode. The SegmentWatcher is notified of the current track via the `/set_track` API (called manually or from an OBS script).

```
OBS → RTMP :1935 → nginx-rtmp → [webhook] → supervisor → FFmpeg → HLS
```

### DB FALLBACK Mode (schedule available for today)

At startup, and at each reload (midnight or `/reload_schedule`), the supervisor loads from MariaDB the list of tracks scheduled for the current day, sorted by `ordre`. Tracks are played in that order. Title and artist come directly from the database — `mutagen` is not used.

When the queue is exhausted during the day, the supervisor automatically switches to random mode for the remainder of the day.

```
MariaDB (today's schedule)
  └── Playlist._queue (ordered)
        └── AudioPipe → FFmpeg HLS
```

### Random FALLBACK Mode (no schedule defined)

If the `programmation` table has no entries for today, or if PyMySQL is missing, or if the MariaDB connection fails, the supervisor falls back to shuffle playback from `/opt/webradio/music/`. Files are shuffled (Fisher-Yates) with a guarantee that the last played track does not appear at the head of the new list. Title and artist tags are read via `mutagen` if available, otherwise the filename without extension is used.

```
/opt/webradio/music/*.m4a  (shuffled)
  └── Playlist._queue (random)
        └── AudioPipe → FFmpeg HLS
```

### Mode Transition Diagram

```
Startup
  └── Load DB schedule
        ├── DB available and non-empty → DB FALLBACK
        └── DB empty or unavailable   → Random FALLBACK

OBS connects (on_publish)
  └── Cancel any pending grace delay
        └── → LIVE

OBS disconnects (on_unpublish)
  └── 8 s grace delay
        ├── OBS reconnects within 8 s → stays LIVE
        └── Timeout → FALLBACK (DB or random depending on the day)

DB schedule exhausted mid-day
  └── → Random FALLBACK (rest of the day)

Midnight
  └── Reload DB schedule for the new day
        ├── Schedule found → DB FALLBACK
        └── Empty          → Random FALLBACK
```

---

## Supervisor Internal API

The supervisor exposes an HTTP server on `127.0.0.1:8089` (not publicly accessible).

### Incoming Webhooks (called by shell scripts)

| Method | Route | Called by | Effect |
|---|---|---|---|
| `POST` | `/on_publish` | `on_publish.sh` | Triggers switch → LIVE |
| `POST` | `/on_unpublish` | `on_unpublish.sh` | Starts grace delay → FALLBACK |

### Control and Monitoring API

| Method | Route | Description |
|---|---|---|
| `GET` | `/status` | Full JSON status |
| `PUT` | `/set_track` | Override title/artist in LIVE mode |
| `POST` | `/reload_schedule` | Reload DB schedule immediately |
| `GET` | `/schedule` | View remaining playback queue for today |

#### GET /status

```json
{
  "mode": "fallback",
  "live": false,
  "listeners": 0,
  "segments": 142,
  "current_title": "Blue in Green",
  "current_artist": "Miles Davis",
  "playlist_source": "db",
  "last_publish": 1718000000.0,
  "last_unpublish": 1718003600.0,
  "ffmpeg_pid": 12345
}
```

Notable fields:

- `mode`: `"live"` | `"fallback"` | `"starting"`
- `playlist_source`: `"db"` (MariaDB schedule) | `"random"` (music folder)
- `ffmpeg_pid`: `null` if FFmpeg is not currently running

#### PUT /set_track

Updates the ID3 metadata injected into upcoming HLS segments. Useful in LIVE mode to display the track currently broadcast by OBS or Mixxx.

```bash
curl -X PUT http://127.0.0.1:8089/set_track \
     -H "Content-Type: application/json" \
     -d '{"title": "So What", "artist": "Miles Davis"}'
```

Response:
```json
{"ok": true, "title": "So What", "artist": "Miles Davis"}
```

#### POST /reload_schedule

Forces an immediate reload of today's schedule without restarting the supervisor. Call this after inserting or modifying entries in the `programmation` table for the current date.

```bash
curl -X POST http://127.0.0.1:8089/reload_schedule
```

Response:
```json
{"ok": true, "date": "2025-06-01", "source": "db", "queued": 24}
```

#### GET /schedule

Returns the remaining playback queue for today without consuming it.

```bash
curl http://127.0.0.1:8089/schedule
```

Response:
```json
{
  "date": "2025-06-01",
  "source": "db",
  "count": 3,
  "items": [
    {"position": 1, "title": "Autumn Leaves", "artist": "Bill Evans", "file": "autumn_leaves.m4a"},
    {"position": 2, "title": "Waltz for Debby", "artist": "Bill Evans", "file": "waltz_debby.m4a"},
    {"position": 3, "title": "Peace Piece", "artist": "Bill Evans", "file": "peace_piece.m4a"}
  ]
}
```

---

## HLS Segment Format

Segments are MPEG-TS files (`seg00001.ts`, `seg00002.ts`…) containing:

- AAC-LC audio, 192 kbps, 44100 Hz, stereo
- An ID3v2.3 tag injected by `ts_inject` with `TIT2` (title) and `TPE1` (artist) frames
- A patched PMT with `stream_type=0x15` and `PID=0x0015` for the metadata track

The `.m3u8` playlist is updated continuously with a 10-segment window (30 seconds of buffer). Old segments are deleted automatically (`delete_segments`).

---

## File Tree

```
/opt/webradio/
├── hls/                    # HLS segments generated by FFmpeg
│   ├── stream.m3u8         # live HLS playlist
│   ├── seg00001.ts
│   └── ...
├── music/                  # audio library (normalised .m4a files)
├── logs/
│   ├── supervisor.log
│   └── nginx-error.log
└── scripts/
    ├── on_publish.sh
    └── on_unpublish.sh
```
