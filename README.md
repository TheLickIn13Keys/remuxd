# remuxd

On-demand remux/transcode of arbitrary video URLs to seekable **HLS**.

Point it at any `http(s)` MKV/MP4/WebM and it probes the source, decides whether
the browser can **stream-copy** the codecs (fast, lossless) or must **transcode**,
then serves a seekable HLS stream — plus extracted text subtitles and embedded
fonts. It's a small, dependency-free HTTP service (stdlib only; needs `ffmpeg`
and `ffprobe` on `PATH`).

## How it decides

For each source, `remuxd` picks one of three strategies:

| Strategy | When | What it does |
|----------|------|--------------|
| **passthrough** | already browser-native (MP4 / 8-bit H.264 / AAC) | proxies the bytes, no ffmpeg |
| **seek-copy** | browser-decodable codec + an MKV Cues index | synthesizes a VOD playlist and produces each keyframe-aligned fMP4 fragment on demand from HTTP byte-range reads — lossless, instant seeking |
| **seek-transcode** | must transcode (Hi10, HDR, undecodable codec, or forced) + an MKV Cues index | same on-demand VOD machinery, but re-encodes each window to 8-bit H.264 — full random-access seeking on transcoded output |
| **singlepass** | must transcode/copy but no usable index | one long-running ffmpeg producing live MPEG-TS HLS (seekable once it finishes) |

10-bit H.264 (Hi10) is the one AVC case no browser can decode, so it's transcoded;
HEVC is copied (plays on Safari/Apple). The two **seek-** paths give a full seek bar
immediately; **singlepass** is the fallback when the source has no keyframe index.

## Install

```sh
pip install -e .            # from this directory
# ffmpeg + ffprobe must be on PATH (a libass-enabled build is needed for hardsub)
```

## Run

```sh
remuxd                      # headless API on http://127.0.0.1:8000
remuxd --demo               # also serve the browser test player at /
remuxd --host 0.0.0.0 -p 9000
```

## Use it as a backend

The engine is a plain HTTP API — any HLS client (hls.js, Safari `<video>`, an
iOS/tvOS player, your own frontend) can drive it. All URLs are scoped to the
session id returned by `/start`, so many clients stream concurrently.

```sh
curl 'http://127.0.0.1:8000/start?src=<url-encoded-mkv-url>&mode=remux'
```

```jsonc
{
  "sid": "a1b2c3d4e5f6",
  "mode": "seek-copy",
  "video": "hevc", "audio": "aac",
  "video_action": "copy (hevc) · 214 keyframe segments",
  "playlist": "/hls/a1b2c3d4e5f6/index.m3u8",   // point your HLS player here
  "subs": "/subs/a1b2c3d4e5f6",                   // base for subtitle tracks
  "tracks":  [ { "url": "/subs/a1b2c3d4e5f6/0", "label": "English", "lang": "eng", "default": true } ],
  "fontlist": "/fontlist/a1b2c3d4e5f6",           // embedded MKV fonts (JSON list)
  "subwindow": "/subwindow/a1b2c3d4e5f6",         // on-demand subs around a seek
  "audios":  [ { "index": 1, "label": "Japanese", "lang": "jpn", "default": true } ]
}
```

### Endpoints

| Endpoint | Purpose |
|----------|---------|
| `GET /start?src=&mode=&headers=&audio=` | probe + start a stream → JSON descriptor |
| `GET /hls/<sid>/index.m3u8` `init.mp4` `seg_N.m4s` | the HLS stream |
| `GET /proxy/<sid>` | direct-play passthrough (Range-forwarded) |
| `GET /subs/<sid>/<n>` | extracted ASS for track `n` (Range support) |
| `GET /subwindow/<sid>?n=&t=` | on-demand ASS around a seek position (seek-copy) |
| `GET /fonts/<sid>/<name>`, `GET /fontlist/<sid>` | embedded MKV fonts for the renderer |
| `GET /resolve?anilist=<id>` | optional resolver plugin (if configured) |

- `mode`: `remux` (copy, default) · `auto` · `transcode`
- `headers`: URL-encoded JSON of upstream request headers (auth, etc.)
- `audio`: absolute audio stream index to select

Subtitles come back as standard ASS — render them however you like (the demo
uses JASSUB, but that's just the demo's choice).

## Configuration (env)

| Var | Default | Meaning |
|-----|---------|---------|
| `REMUXD_HOST` / `REMUXD_PORT` (or `PORT`) | `127.0.0.1` / `8000` | bind address |
| `FFMPEG_BIN` / `FFPROBE_BIN` | `ffmpeg` / `ffprobe` | binary paths |
| `REMUXD_VIDEO_ENCODE` | `-c:v libx264 -preset veryfast -crf 21 -pix_fmt yuv420p` | full video-codec spec for the transcode paths. Swap in a hardware encoder, e.g. Intel Quick Sync `-c:v h264_qsv -b:v 6M`, NVENC `-c:v h264_nvenc -b:v 6M`, or macOS `-c:v h264_videotoolbox -b:v 6M -pix_fmt yuv420p` |
| `REMUXD_SESSION_ROOT` | `./.remuxd-sessions` | per-session working dirs |
| `REMUXD_SEGMENT_SECONDS` | `6` | HLS target segment length |
| `REMUXD_PREFETCH_SEGMENTS` | `32` | fragments to keep produced **ahead of the playhead** (seek). Set very high to download the whole file ahead; `0` disables read-ahead |
| `REMUXD_FRAGMENT_CACHE_MB` | `512` | per-session cap on cached fragment bytes (bounds memory at high read-ahead; farthest-from-playhead fragments evicted first) |
| `REMUXD_PREFETCH_CONCURRENCY` | `6` | global cap on concurrent prefetch ffmpeg jobs (on-demand requests are never throttled) |
| `REMUXD_SESSION_TTL` | `1800` | idle seconds before a session is reaped |
| `REMUXD_PREP_CACHE_TTL` | `120` | seconds to memoize per-source prep (resolved URL, probe, cues index, header) so a repeat `/start` — audio switch, replay — skips it; `0` disables |
| `REMUXD_MAX_SESSIONS` | `32` | concurrency cap (0 = unlimited) |
| `REMUXD_USER_AGENT` | Chrome UA | UA for upstream fetches |
| `REMUXD_LOG_LEVEL` | `INFO` | `DEBUG`/`INFO`/`WARNING`/`ERROR` |

## Read-ahead / pre-buffering

For the **seek** paths (copy and transcode) a background worker produces fragments
*ahead of the playhead* into a bounded per-session cache, so playback and seeks land on
warm fragments instead of waiting on a byte-range fetch + remux. It refills as the
playhead advances and evicts the fragments farthest behind first.

- Default: ~32 fragments (~3 min) ahead, 512 MB/session cache.
- Download the whole file ahead of play: `REMUXD_PREFETCH_SEGMENTS=100000`
  (raise `REMUXD_FRAGMENT_CACHE_MB` too, since the cache is the real bound).
- Disable and go back to strictly on-demand: `REMUXD_PREFETCH_SEGMENTS=0`.

(The **passthrough** path leaves buffering to the browser; **singlepass** already
runs ffmpeg faster-than-realtime, so it's inherently buffered ahead.)

## Tuning for your server

Rough guidance — scale to your box:

- **Encoder**: default `libx264` uses the CPU and works anywhere. On a machine with
  Intel Quick Sync (most modern Intel iGPUs) or an NVIDIA GPU, set
  `REMUXD_VIDEO_ENCODE` to `h264_qsv` / `h264_nvenc` to offload transcoding and free
  the CPU. Copy paths (seek-copy, passthrough) don't encode at all.
- **Memory**: worst-case RAM ≈ `REMUXD_MAX_SESSIONS × REMUXD_FRAGMENT_CACHE_MB`.
  Defaults (32 × 512 MB ≈ 16 GB) suit a 32–64 GB box; raise the cache for deeper
  read-ahead if you have headroom.
- **CPU**: `REMUXD_PREFETCH_CONCURRENCY` bounds simultaneous prefetch encodes. With
  software `libx264` on a many-thread CPU, ~half the physical cores is a safe start
  (each x264 encode is itself multi-threaded); with a hardware encoder you can go
  higher. On-demand (the segment you're actually waiting on) is never throttled.

## Resolver plugin (optional)

`remuxd/plugins/anilist.py` maps an AniList id to a ranked list of playable
streams via an [AIOStreams](https://github.com/Viren070/AIOStreams) instance,
mounted at `/resolve` **only when configured**. Set (never commit) credentials:

```sh
export AIO_INSTANCE=https://aiostreams.example.com
export AIO_UUID=...
export AIO_PASS=...
```

Results are memoized for `AIO_CACHE_TTL` seconds (default 300; `0` disables), so
re-opening or re-picking a title skips the slow debrid search.

## Notes

- Single-file, stdlib-only server (`http.server`) — no framework, no runtime deps.- Sessions are reaped on idle and on shutdown; working dirs are cleaned up.
- For public exposure, front it with a reverse proxy (TLS, auth, rate limiting)
  and pin `REMUXD_MAX_SESSIONS` to your ffmpeg/CPU budget.
