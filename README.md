# remuxd

On-demand remux/transcode of arbitrary video URLs to seekable **HLS**.

Point it at any `http(s)` MKV/MP4/WebM and it probes the source, decides whether
the browser can **stream-copy** the codecs (fast, lossless) or must **transcode**,
then serves a seekable HLS stream, plus extracted text subtitles and embedded
fonts. It's a small, dependency-free HTTP service (stdlib only; needs `ffmpeg`
and `ffprobe` on `PATH`).

## How it decides

For each source, `remuxd` picks one of three strategies:

| Strategy | When | What it does |
|----------|------|--------------|
| **passthrough** | already browser-native (MP4 / 8-bit H.264 / AAC) | proxies the bytes, no ffmpeg |
| **seek-copy** | browser-decodable codec + an MKV Cues index | synthesizes a VOD playlist and produces each keyframe-aligned fMP4 fragment on demand from HTTP byte-range reads: lossless, instant seeking |
| **seek-transcode** | must transcode (Hi10, HDR, undecodable codec, or forced) + an MKV Cues index | same on-demand VOD machinery, but re-encodes each window to 8-bit H.264, giving full random-access seeking on transcoded output |
| **singlepass** | must transcode/copy but no usable index | one long-running ffmpeg producing live MPEG-TS HLS (seekable once it finishes) |

10-bit H.264 (Hi10) is the one AVC case no browser can decode, so it's transcoded;
HEVC is copied (plays on Safari/Apple). The two **seek-** paths give a full seek bar
immediately; **singlepass** is the fallback when the source has no keyframe index.

## Install

```sh
pip install -e .            # from this directory
# ffmpeg + ffprobe must be on PATH
```

## Run

```sh
remuxd                      # headless API on http://127.0.0.1:8000
remuxd --demo               # also serve the browser test player at /
remuxd --host 0.0.0.0 -p 9000
remuxd --session-root /var/tmp/remuxd --log-level DEBUG
```

Every flag has an env-var equivalent (below); flags win.

### Docker

```sh
docker build -t remuxd .
docker run --rm -p 127.0.0.1:8000:8000 remuxd
docker run --rm -p 127.0.0.1:8000:8000 --env-file .env -e REMUXD_DEMO=1 remuxd
```

`docker-compose.yml` brings up remuxd together with the
[AniBridge](#id-mapping-fallbacks) mapping service on one network, which is the
easiest way to run the resolver:

```sh
docker compose up -d --build     # first anibridge boot takes ~2 min
docker compose logs -f remuxd
docker compose down
```

It reads `.env` for credentials and tuning, and sets only what the two-container
layout dictates: `ANIBRIDGE_URL=http://anibridge:4848` (service name, not
`127.0.0.1`), the demo on, session scratch on a tmpfs, and
`REMUXD_FONTS_DIR` (default `./fonts`) mounted read-only for
[subtitle fonts](#subtitle-fonts). AniBridge's own database lives in
`./.anibridge/data`, so restarts don't re-download it.

The image bundles `ffmpeg`/`ffprobe`, runs unprivileged, and defaults
`REMUXD_HOST` to `0.0.0.0` (the `127.0.0.1` default would be unreachable from
outside the container). Session scratch lives in `/var/lib/remuxd/sessions`; add
`--tmpfs /var/lib/remuxd/sessions:size=4g,mode=1777` to keep it off disk — the
`mode` is not optional, since a tmpfs mounts root-owned `755` over that path and
hides the ownership the image set up, leaving the unprivileged user unable to
create a session (`Permission denied: '/var/lib/remuxd/sessions/<sid>'`).
`docker stop` is a clean shutdown: remuxd runs as PID 1 and reaps sessions on
`SIGTERM`.

**Fonts.** Releases that embed no fonts and just name system ones (see
[Subtitle fonts](#subtitle-fonts)) need those fonts present in the container,
which ships with only a minimal set. `/usr/share/fonts` is scanned recursively,
so mount yours into a *subdirectory* of it; that keeps the image's own fonts
visible rather than hiding them behind the mount:

```sh
docker run --rm -p 127.0.0.1:8000:8000 \
  -v /usr/share/fonts:/usr/share/fonts/host:ro \
  remuxd
```

On macOS use `~/Library/Fonts` (or any folder you control) as the source;
`/System/Library/Fonts` is on the read-only system volume and Docker Desktop
can't share it.

## Use it as a backend

The engine is a plain HTTP API: any HLS client (hls.js, Safari `<video>`, an
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

Abridged; the response also carries `pix_fmt`, `container` and `audio_action`.
`subs`, `fontlist` and `subwindow` are `null` when the source has no text
subtitles (and `subwindow` also on the non-seek paths), so check before using.

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
| `POST /stop/<sid>` | tear the session down now (kills prefetch/ffmpeg, frees the cache) |

- `mode`: `remux` (copy, default) · `auto` · `transcode`
- `headers`: JSON object of upstream request headers (auth, etc.), URL-encoded
  as a query value
- `audio`: absolute audio stream index to select
- `/start` answers **502** if the source can't be probed or opened, and **503**
  when `REMUXD_MAX_SESSIONS` is reached with every session still active.

Subtitles come back as standard ASS, so render them however you like (the demo
uses JASSUB, but that's just the demo's choice).

### Subtitle fonts

`/fontlist/<sid>` returns every font a client needs to render the ASS correctly.
It has two sources:

1. **Fonts embedded in the MKV** (attachments), extracted alongside the
   subtitles. This covers most fansubbed releases.
2. **Fonts installed on the remuxd host.** Many web sources embed nothing and
   just name a system font (`Trebuchet MS`, `Verdana`). A native player resolves
   those from the OS, but a browser-side libass/WASM renderer only sees what we
   serve it, so remuxd looks them up in the usual font directories and serves
   any it finds.

Source 2 makes rendering depend on the host: the same release looks different on
a machine with the font installed than on one without. Matching is by **filename**,
so metric-compatible substitutes don't count: a style naming `Arial` will not
resolve against `LiberationSans-Regular.ttf`. Install the actual families you
care about, or accept the renderer's fallback. Under Docker this means mounting
a font directory (see [Docker](#docker)).

## Configuration (env)

| Var | Default | Meaning |
|-----|---------|---------|
| `REMUXD_HOST` / `REMUXD_PORT` (or `PORT`) | `127.0.0.1` / `8000` | bind address |
| `FFMPEG_BIN` / `FFPROBE_BIN` | `ffmpeg` / `ffprobe` | binary paths |
| `REMUXD_VIDEO_ENCODE` | `-c:v libx264 -preset veryfast -crf 21 -pix_fmt yuv420p` | full video-codec spec for the transcode paths. Swap in a hardware encoder, e.g. Intel Quick Sync `-c:v h264_qsv -b:v 6M`, NVENC `-c:v h264_nvenc -b:v 6M`, or macOS `-c:v h264_videotoolbox -b:v 6M -pix_fmt yuv420p` |
| `REMUXD_SESSION_ROOT` | `./.remuxd-sessions` | per-session working dirs |
| `REMUXD_SEGMENT_SECONDS` | `10` | HLS target segment length. Rate limiters count requests, not bytes, so this is the biggest lever on upstream 429s: longer segments mean proportionally fewer requests for the same video, at the cost of coarser seeking |
| `REMUXD_PREFETCH_SEGMENTS` | `32` | fragments to keep produced **ahead of the playhead** (seek). Set very high to download the whole file ahead; `0` disables read-ahead |
| `REMUXD_FRAGMENT_CACHE_MB` | `1024` | per-session cap on cached fragment bytes (farthest-from-playhead evicted first). Size it to cover `PREFETCH_SEGMENTS × SEGMENT_SECONDS × bitrate`, or the cache evicts fragments read-ahead just fetched and they get re-downloaded |
| `REMUXD_PREFETCH_CONCURRENCY` | `4` | global cap on concurrent prefetch ffmpeg jobs (on-demand requests are never throttled). Mainly controls burstiness at session start; adaptive pacing governs the steady-state rate |
| `REMUXD_SESSION_TTL` | `1800` | idle seconds before a session is reaped |
| `REMUXD_PREP_CACHE_TTL` | `120` | seconds to memoize per-source prep (resolved URL, probe, cues index, header) so a repeat `/start` (audio switch, replay) skips it; `0` disables |
| `REMUXD_MAX_SESSIONS` | `32` | concurrency cap (0 = unlimited). At the cap, sessions idle >60 s are evicted LRU-first; if every session is active, `/start` answers **503** instead of killing a live stream |
| `REMUXD_USER_AGENT` | Chrome UA | UA for upstream fetches |
| `REMUXD_CORS_ORIGIN` | *(unset)* | if set (e.g. `https://player.example.com` or `*`), all responses carry `Access-Control-Allow-Origin` and `OPTIONS` preflights are answered; needed when a frontend on another origin drives the API |
| `REMUXD_DEMO` | *(unset)* | `1`/`true`/`yes` serves the browser demo player at `/` (same as `--demo`) |
| `REMUXD_LOG_LEVEL` | `INFO` | `DEBUG`/`INFO`/`WARNING`/`ERROR` |

## Read-ahead / pre-buffering

For the **seek** paths (copy and transcode) a background worker produces fragments
*ahead of the playhead* into a bounded per-session cache, so playback and seeks land on
warm fragments instead of waiting on a byte-range fetch + remux. It refills as the
playhead advances and evicts the fragments farthest behind first.

- Default: ~32 fragments (~5 min) ahead, 1 GB/session cache.
- Download the whole file ahead of play: `REMUXD_PREFETCH_SEGMENTS=100000`
  (raise `REMUXD_FRAGMENT_CACHE_MB` too, since the cache is the real bound).
- Disable and go back to strictly on-demand: `REMUXD_PREFETCH_SEGMENTS=0`.

(The **passthrough** path leaves buffering to the browser; **singlepass** already
runs ffmpeg faster-than-realtime, so it's inherently buffered ahead.)

## Tuning for your server

Rough starting points; scale to your box:

- **Encoder**: default `libx264` uses the CPU and works anywhere. On a machine with
  Intel Quick Sync (most modern Intel iGPUs) or an NVIDIA GPU, set
  `REMUXD_VIDEO_ENCODE` to `h264_qsv` / `h264_nvenc` to offload transcoding and free
  the CPU. Copy paths (seek-copy, passthrough) don't encode at all.
- **Memory**: worst-case RAM ≈ `REMUXD_MAX_SESSIONS × REMUXD_FRAGMENT_CACHE_MB`.
  This is a cap, not an allocation: it only fills if read-ahead actually gets that
  far. Defaults (32 × 1 GB) assume you won't run 32 fully-buffered sessions at once;
  lower `REMUXD_MAX_SESSIONS` if you might.
- **CPU**: `REMUXD_PREFETCH_CONCURRENCY` bounds simultaneous prefetch encodes. With
  software `libx264` on a many-thread CPU, ~half the physical cores is a safe start
  (each x264 encode is itself multi-threaded); with a hardware encoder you can go
  higher. On-demand (the segment you're actually waiting on) is never throttled.

## Upstream rate limits (429)

Debrid/CDN hosts rate-limit by **request count**, and read-ahead is deliberately
request-hungry. Every large ranged read for a session — fragments, prefetch and
on-demand subtitle windows — goes through one throttle: a 429/503 pauses the whole
session (honoring `Retry-After`), and the read-ahead pace rises on refusal and
decays on success, so it settles under the host's limit instead of sprinting into
it every few seconds. A segment a player is waiting on rides the backoff out and
retries; if it still can't be served, the player gets **503 + Retry-After**.

Read the log level, not the word "refused". `[throttle …] upstream refused` at
**INFO** is the pacing doing its job — nothing to do. At **WARNING** the backoff
is doubling, i.e. the host is refusing faster than we back off: raise
`REMUXD_SEGMENT_SECONDS` (fewer, larger requests) first, and lower
`REMUXD_PREFETCH_CONCURRENCY` if the refusals cluster at session start.

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

### Id-mapping fallbacks

The anime api on the AIOStreams instance doesn't know every AniList id, and
searching by bare AniList id usually returns nothing. When it can't map an id
(or is down), the resolver tries two more mappers before giving up:

**1. A local [AniBridge](https://anibridge.eliasbenb.dev) instance**, serving the
[anibridge-mappings](https://github.com/anibridge/anibridge-mappings) dataset —
~19k AniList entries with episode-accurate links to IMDb, TMDB and TVDB,
refreshed daily. Run it alongside remuxd; it needs no configuration of its own,
we only read its mappings api:

```sh
docker run -d --name anibridge --restart unless-stopped \
  -e PUID=$(id -u) -e PGID=$(id -g) -e TZ=Etc/UTC \
  -p 4848:4848 -v ./.anibridge/data:/config \
  ghcr.io/anibridge/anibridge:v2
```

First boot takes a minute or two while it downloads the mappings database; its
own dashboard is at <http://127.0.0.1:4848>. If remuxd itself runs in a
container, `127.0.0.1` is *that* container — set
`ANIBRIDGE_URL=http://host.docker.internal:4848` (Docker Desktop), or put both on
one docker network and use `http://anibridge:4848`. The dataset carries no IMDb
ids for series, so series resolve to `tmdb:<id>:<season>:<episode>` (or `tvdb:`),
which AIOStreams searches happily; films resolve to their IMDb id.

**2. The [Kometa Anime-IDs](https://github.com/Kometa-Team/Anime-IDs)
snapshot**, a ~1.7 MB JSON file downloaded once and cached on disk. It carries
IMDb ids for only ~1500 titles (mostly films), but needs no extra service, so
it's the fallback that works when AniBridge isn't running.

| var | default | |
|---|---|---|
| `ANIBRIDGE_URL` | `http://127.0.0.1:4848` | instance base url |
| `ANIBRIDGE_USER` / `ANIBRIDGE_PASS` | unset | only if it has basic auth on |
| `ANIBRIDGE_TIMEOUT` | `5` | per-request seconds |
| `ANIBRIDGE_CACHE_TTL` | `3600` | seconds to memoize mappings (`0` = off) |
| `ANIBRIDGE_DISABLE` | unset | `1` skips this fallback |
| `ANIME_IDS_URL` | the repo's `anime_ids.json` | snapshot source |
| `ANIME_IDS_CACHE` | `<tmp>/remuxd-anime-ids.json` | on-disk copy |
| `ANIME_IDS_TTL` | `86400` | seconds before refetch (`0` = never) |
| `ANIME_IDS_DISABLE` | unset | `1` skips this fallback |

An unreachable AniBridge is not fatal, and never sticky. A refused connection
costs nothing, so it's simply retried on the next request — the first `/resolve`
after the container is back resolves normally. Only a *timeout* arms a 60-second
skip window, so a hung host can't slow every resolve down. Neither outcome is
cached: `/resolve` memoizes results for `AIO_CACHE_TTL`, but only when the search
actually found streams, so a blip can't leave you looking at "0 streams" until
something restarts.

## Session lifecycle

A session holds a fragment cache, a read-ahead worker and possibly a live ffmpeg,
so it's worth ending them deliberately:

- `POST /stop/<sid>` when the user switches away from a stream. Otherwise it
  lingers until `REMUXD_SESSION_TTL`. As a safety net the read-ahead worker
  pauses after 60 s with no client request, so an abandoned session stops
  downloading even if `/stop` is never called.
- Sessions are also reaped on idle and on shutdown, and their working dirs
  removed. The session **root** is only removed if empty, so pointing
  `REMUXD_SESSION_ROOT` at an existing directory is safe.
- Full-file subtitle/font extraction is lazy: it starts on the first `/subs` or
  `/fontlist` hit, so clients that never ask for subs don't trigger a whole-file
  read. `/subwindow` works independently of it.

## Notes

- Single-file, stdlib-only server (`http.server`): no framework, no runtime deps.
- `src` must be an `http(s)` URL; other schemes are rejected (`file://` would be a
  local-file read). Pass it URL-encoded **once**.
- Segments, `init.mp4` and seekable playlists are served immutable; growing
  resources (live playlists, subs mid-extraction) are `no-store`. `HEAD` works on
  the read-only endpoints.
- **remuxd fetches whatever URL it is given.** It has no authentication of its
  own, so anything that can reach it can make it issue requests from your
  network. Bind to localhost, or front it with a reverse proxy (TLS, auth, rate
  limiting) and pin `REMUXD_MAX_SESSIONS` to your ffmpeg/CPU budget.
