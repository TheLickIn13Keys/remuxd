"""Subtitle extraction and descriptors.

Text tracks are extracted to ``.ass`` out-of-band from the video HLS (so playback
starts fast and subs stream in behind it), preserving signs/karaoke/typesetting.
Embedded MKV fonts are dumped alongside. Client URLs are scoped by session id.
"""
import logging
import os
import re
import shutil
import subprocess
import threading
from typing import Dict, List, Optional

from . import mkvcues
from .netio import header_args

log = logging.getLogger("remuxd.subtitles")

_RECONNECT = ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5"]


def sub_tracks(subs: List[dict], out_dir: str) -> List[dict]:
    """Probed subtitle streams -> track descriptors. Default = first English track,
    else the first."""
    tracks = []
    for n, s in enumerate(subs):
        lang = s.get("lang") or ""
        title = s.get("title") or ""
        label = title or (lang.upper() if lang else f"Track {n + 1}")
        tracks.append({"index": s["index"],
                       "path": os.path.join(out_dir, f"subs_{n}.ass"),
                       "lang": lang, "label": label, "default": False})
    dflt = next((t for t in tracks
                 if t["lang"] in ("eng", "en") or "english" in t["label"].lower()), None)
    if dflt is None and tracks:
        dflt = tracks[0]
    if dflt:
        dflt["default"] = True
    return tracks


def track_meta(tracks: List[dict], sid: str) -> List[dict]:
    """Frontend-facing descriptors with sid-scoped URLs."""
    return [{"url": f"/subs/{sid}/{n}", "label": t["label"], "lang": t["lang"],
             "default": t["default"]} for n, t in enumerate(tracks)]


def dump_args(attachments: List[dict]) -> List[str]:
    """ffmpeg args to dump each attachment stream to an explicit, sanitized
    filename (relative to the ffmpeg cwd = the fonts dir). Explicit-per-stream
    rather than the blanket ``-dump_attachment:t ""``, which trusts the MKV's
    declared filenames; a crafted ``../../name`` would escape the fonts dir."""
    args: List[str] = []
    seen = set()
    for n, att in enumerate(attachments or []):
        raw = (att.get("filename") or "").replace("\\", "/")
        name = os.path.basename(raw)
        if not name or name in (".", "..") or name in seen:
            name = f"attachment_{n}{os.path.splitext(name)[1]}"
        seen.add(name)
        args += [f"-dump_attachment:t:{n}", name]
    return args


# Where installed fonts live on the machine running remuxd (macOS + Linux).
_SYSTEM_FONT_DIRS = [
    "/System/Library/Fonts", "/Library/Fonts", os.path.expanduser("~/Library/Fonts"),
    "/usr/share/fonts", "/usr/local/share/fonts",
    os.path.expanduser("~/.local/share/fonts"), os.path.expanduser("~/.fonts"),
]
_FONT_EXTS = (".ttf", ".otf", ".ttc")
# Cap per root, so a huge mounted tree can't turn this into a full-disk walk.
_MAX_FONT_SCAN = 20000
_font_index_cache = {}


def ass_fontnames(text: str) -> set:
    """Font family names an ASS script renders with: the Fontname field of each
    Style line, plus inline ``\\fn`` overrides."""
    names = set()
    for line in text.splitlines():
        if line.startswith("Style:"):
            fields = line[len("Style:"):].split(",")
            if len(fields) >= 2 and fields[1].strip():
                names.add(fields[1].strip().lstrip("@"))   # @ = vertical layout
    for m in re.finditer(r"\\fn@?([^\\}\r\n]+)", text):
        if m.group(1).strip():
            names.add(m.group(1).strip())
    return names


_FONT_VARIANTS = {"", "regular", "bold", "italic", "oblique", "bolditalic",
                  "boldoblique", "bd", "bi", "it", "i", "b"}


def _font_index() -> List[tuple]:
    """[(normalized_stem, path)] for every installed font file, built once per
    distinct _SYSTEM_FONT_DIRS. Walks recursively: macOS keeps fonts flat in
    /System/Library/Fonts, but Linux nests them (Debian puts everything under
    /usr/share/fonts/truetype/<package>/), so a flat listdir finds nothing
    there. A mounted font directory is usually nested too."""
    key = tuple(_SYSTEM_FONT_DIRS)
    cached = _font_index_cache.get(key)
    if cached is not None:
        return cached
    index = []
    for d in _SYSTEM_FONT_DIRS:
        seen = 0
        # os.walk swallows errors on missing/unreadable dirs by default
        for root, _dirs, files in os.walk(d):
            for fn in files:
                stem, ext = os.path.splitext(fn)
                if ext.lower() in _FONT_EXTS:
                    index.append((re.sub(r"[\s_-]+", "", stem).lower(),
                                  os.path.join(root, fn)))
            seen += len(files)
            if seen >= _MAX_FONT_SCAN:
                log.debug("font scan of %s hit the %d-entry cap", d, _MAX_FONT_SCAN)
                break
    _font_index_cache[key] = index
    return index


def _find_system_fonts(family: str) -> List[str]:
    """Installed font files for a family, matched by filename ("Trebuchet MS"
    -> "Trebuchet MS.ttf" + its Bold/Italic siblings). Filename matching is a
    heuristic, but over-matching is harmless: libass picks fonts by their
    internal name table, so an extra file is just ignored."""
    want = re.sub(r"[\s_-]+", "", family).lower()
    return [path for norm, path in _font_index()
            if norm.startswith(want) and norm[len(want):] in _FONT_VARIANTS]


def _dump_style_fonts(url: str, fonts_dir: str,
                      headers: Optional[Dict[str, str]] = None) -> int:
    """Serve locally-installed fonts for the families the ASS styles reference.
    Many web sources (e.g. CR WEB-DLs) embed NO fonts and just name system
    fonts like "Trebuchet MS". Native players resolve those from the OS, but
    the browser-side libass/WASM renderer only sees fonts we serve, so it
    silently falls back to its default. Reads the styles from the subtitle
    tracks' CodecPrivate (in the MKV header, no full-file read)."""
    try:
        privates = mkvcues.fetch_subtitle_privates(url, headers)
    except Exception as e:
        log.debug("subtitle CodecPrivate fetch failed: %s", e)
        return 0
    families = set()
    for priv in privates:
        families |= ass_fontnames(priv.decode("utf-8", "replace"))
    copied = 0
    for fam in sorted(families):
        for src in _find_system_fonts(fam):
            dst = os.path.join(fonts_dir, os.path.basename(src))
            if os.path.exists(dst):
                continue
            try:
                shutil.copyfile(src, dst)
                copied += 1
            except OSError as e:
                log.debug("could not copy font %s: %s", src, e)
    if copied:
        log.info("served %d system font files for %d style families",
                 copied, len(families))
    return copied


def extract_all(ffmpeg: str, url: str, tracks: List[dict], fonts_dir: str,
                headers: Optional[Dict[str, str]] = None,
                ua: Optional[str] = None, refresh_url=None,
                attachments: Optional[List[dict]] = None) -> None:
    """Extract all text tracks as .ass and dump embedded fonts in ONE pass (a single
    read of the file). Best-effort; meant to run in a background thread.

    ``ua``: browser-like User-Agent for the HTTP fetch; ffmpeg's default Lavf UA
    gets 403'd by some CDNs (every other fetch in remuxd already sends this).
    ``refresh_url``: callable(failed_url) -> fresh URL; extraction runs long after
    /start, so an expired CDN link gets one re-resolve + retry.
    ``attachments``: probed attachment streams, each dumped to a sanitized name."""
    if not tracks:
        return
    os.makedirs(fonts_dir, exist_ok=True)
    # Styles may name fonts the release didn't embed but that exist on this
    # machine; serve those too, so the browser renderer matches native players.
    try:
        _dump_style_fonts(url, fonts_dir, headers)
    except Exception as e:
        log.debug("style font pass failed: %s", e)

    def run(u):
        cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
               *(["-user_agent", ua] if ua else []),
               *header_args(headers), *_RECONNECT,
               *dump_args(attachments or []), "-i", u]
        for t in tracks:
            # -flush_packets 1: write each cue immediately; else the ASS muxer
            # buffers early cues and the file looks empty for ~40s.
            # abspath: ffmpeg runs with cwd=fonts_dir (attachments dump to cwd),
            # so a relative session root would otherwise misplace the .ass files.
            cmd += ["-map", f"0:{t['index']}", "-c:s", "ass", "-flush_packets", "1",
                    os.path.abspath(t["path"])]
        return subprocess.run(cmd, capture_output=True, timeout=1800, cwd=fonts_dir)

    try:
        r = run(url)
        if r.returncode != 0 and refresh_url is not None:
            r = run(refresh_url(url))   # link may have expired mid-extraction
        if r.returncode != 0:
            log.warning("subtitle extraction failed (exit %d): %s", r.returncode,
                        r.stderr.decode(errors="replace").strip()[-500:])
    except Exception as e:
        log.warning("subtitle extraction failed: %s", e)


_WINDOW_CHUNK = 256 * 1024


def extract_window(ffmpeg: str, url: str, header_bytes: bytes, boff: int,
                   bend: Optional[int], track_index: int,
                   headers: Optional[Dict[str, str]] = None,
                   start_time: float = 0.0, open_range=None) -> bytes:
    """On-demand ASS for a byte window: stream clusters [boff,bend) into ffmpeg
    behind the MKV header and extract track_index with -copyts (preserve absolute
    timestamps; without it ffmpeg rebases to 0). Byte-range only, no ffmpeg HTTP
    seek.

    ``open_range``: callable() -> open readable response for those bytes. This is
    the same tens-of-MB fetch a video fragment makes, so the caller injects an
    opener carrying its rate-limit backoff and link-refresh policy; the default
    is a bare ranged GET.

    The range is piped in as it downloads rather than buffered whole first: these
    windows are tens of MB of interleaved video fetched only to recover a few
    kilobytes of text, so demuxing in parallel with the download is most of the
    post-seek latency."""
    # -itsoffset cancels the container's start offset. -copyts alone gives
    # absolute timestamps, but the whole-file pass rebases to the container start
    # and the HLS timeline begins at 0; on a source with start_time != 0 the two
    # disagree, so every cue arrives twice and libass stacks the pair on screen.
    off = ["-itsoffset", f"-{start_time:.6f}"] if start_time else []
    p = subprocess.Popen(
        [ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
         "-copyts", *off, "-i", "pipe:0",
         "-map", f"0:{track_index}", "-c:s", "ass", "-f", "ass", "pipe:1"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    feed_err: List[str] = []

    def feed():
        try:
            p.stdin.write(header_bytes)
            opener = open_range or (lambda: mkvcues.open_range(url, boff, bend, headers))
            with opener() as r:
                while True:
                    chunk = r.read(_WINDOW_CHUNK)
                    if not chunk:
                        break
                    p.stdin.write(chunk)
        except Exception as e:
            # ffmpeg exiting early (it has all the subs it needs) closes the pipe
            # under us; that's a normal finish, not a fetch failure
            log.debug("subwindow feed ended: %s", e)
            # ...but anything other than the pipe going away is a fetch failure,
            # and ffmpeg's stderr only shows the downstream symptom, so carry the
            # real cause through to the warning below.
            if not isinstance(e, (BrokenPipeError, ValueError)):
                feed_err.append(f"{type(e).__name__}: {e}")
        finally:
            try:
                p.stdin.close()
            except Exception:
                pass

    err: List[bytes] = []
    writer = threading.Thread(target=feed, name="subwin-feed", daemon=True)
    drain = threading.Thread(target=lambda: err.append(p.stderr.read()),
                             name="subwin-err", daemon=True)
    writer.start()
    drain.start()
    # stdout.read() blocks until ffmpeg closes the pipe, so the deadline has to be
    # a watchdog rather than a wait() timeout (killing it unblocks the read)
    watchdog = threading.Timer(120, p.kill)
    watchdog.start()
    try:
        out = p.stdout.read()          # ffmpeg's stdout closes when it's done
        p.wait()
    except Exception:
        p.kill()
        p.wait()
        out = b""
    finally:
        watchdog.cancel()
    writer.join(timeout=5)
    drain.join(timeout=5)
    if p.returncode not in (0, None) and not out:
        log.warning("subwindow extract failed (exit %s)%s: %s", p.returncode,
                    f" [feed: {feed_err[0]}]" if feed_err else "",
                    (err[0] if err else b"").decode(errors="replace").strip()[-300:])
    return out
