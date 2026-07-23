"""Subtitle extraction and descriptors.

Text tracks are extracted to ``.ass`` out-of-band from the video HLS (so playback
starts fast and subs stream in behind it), preserving signs/karaoke/typesetting.
Embedded MKV fonts are dumped alongside. Client URLs are scoped by session id.
"""
import logging
import os
import subprocess
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


def extract_all(ffmpeg: str, url: str, tracks: List[dict], fonts_dir: str,
                headers: Optional[Dict[str, str]] = None) -> None:
    """Extract all text tracks as .ass and dump embedded fonts in ONE pass (a single
    read of the file). Best-effort; meant to run in a background thread."""
    if not tracks:
        return
    os.makedirs(fonts_dir, exist_ok=True)
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
           *header_args(headers), *_RECONNECT,
           "-dump_attachment:t", "", "-i", url]
    for t in tracks:
        # -flush_packets 1: write each cue immediately; else the ASS muxer buffers
        # early cues and the file looks empty for ~40s.
        cmd += ["-map", f"0:{t['index']}", "-c:s", "ass", "-flush_packets", "1", t["path"]]
    try:
        subprocess.run(cmd, capture_output=True, timeout=1800, cwd=fonts_dir)
    except Exception as e:
        log.warning("subtitle extraction failed: %s", e)


def extract_window(ffmpeg: str, url: str, header_bytes: bytes, boff: int,
                   bend: Optional[int], track_index: int,
                   headers: Optional[Dict[str, str]] = None) -> bytes:
    """On-demand ASS for a byte window: fetch clusters [boff,bend), prepend the MKV
    header, extract track_index with -copyts (preserve absolute timestamps; without
    it ffmpeg rebases to 0). Byte-range only — no ffmpeg HTTP seek."""
    blob = header_bytes + mkvcues.fetch_range(url, boff, bend, headers)
    p = subprocess.Popen(
        [ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
         "-copyts", "-i", "pipe:0",
         "-map", f"0:{track_index}", "-c:s", "ass", "-f", "ass", "pipe:1"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        out, _ = p.communicate(blob, timeout=120)
    except subprocess.TimeoutExpired:
        p.kill()
        p.communicate()          # reap the killed child (don't orphan it)
        return b""
    return out
