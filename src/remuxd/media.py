"""Media inspection + ffmpeg command construction.

Pure functions over ffprobe/ffmpeg: probe a source, decide copy-vs-transcode,
build the argv for the HLS remux and per-segment fragment remux. No global state.
"""
import json
import logging
import os
import subprocess
from typing import Dict, List, Optional, Tuple

from .netio import header_args

log = logging.getLogger("remuxd.media")

TEXT_SUBS = {"ass", "ssa", "subrip", "srt", "mov_text", "webvtt", "text"}
_RECONNECT = ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5"]
# Portable default video encoder for the transcode paths (override via Config).
DEFAULT_VENC = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p"]


def probe(ffprobe: str, url: str, headers: Optional[Dict[str, str]] = None,
          ua: Optional[str] = None) -> Tuple:
    """One ffprobe call -> (vcodec, pix_fmt, acodec, container, subs, aidx, audios,
    duration, attachments). subs = TEXT subtitle streams (image subs skipped);
    audios = all audio streams; aidx/acodec = the default chosen audio track (see
    choose_audio); attachments = embedded files (fonts) with their declared names."""
    h = header_args(headers)
    if ua:
        h = ["-user_agent", ua, *h]   # some CDNs 403 the default Lavf UA
    r = subprocess.run(
        [ffprobe, "-v", "error", *h, *_RECONNECT,
         "-show_entries",
         "stream=index,codec_type,codec_name,pix_fmt:stream_tags=language,title,filename:format=format_name,duration",
         "-of", "json", url],
        capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        # Surface the real error (dead link, 403, bad file) now, instead of an
        # empty probe routing to singlepass and failing 30s later.
        tail = (r.stderr or "").strip()[-500:]
        raise RuntimeError(f"ffprobe failed (exit {r.returncode}): {tail}")
    try:
        data = json.loads(r.stdout or "{}")
    except json.JSONDecodeError:
        data = {}
    vcodec = pixfmt = ""
    subs: List[dict] = []
    audios: List[dict] = []
    attachments: List[dict] = []
    for st in data.get("streams", []):
        t = st.get("codec_type")
        tags = st.get("tags") or {}
        lang = (tags.get("language") or "").lower()
        if t == "video" and not vcodec:
            vcodec = st.get("codec_name", "")
            pixfmt = st.get("pix_fmt", "")
        elif t == "audio":
            audios.append({"index": st.get("index"), "lang": lang,
                           "codec": st.get("codec_name", ""),
                           "title": tags.get("title") or ""})
        elif t == "subtitle" and st.get("codec_name") in TEXT_SUBS:
            subs.append({"index": st.get("index"), "lang": lang,
                         "title": tags.get("title") or ""})
        elif t == "attachment":
            attachments.append({"index": st.get("index"),
                                "filename": tags.get("filename") or ""})
    chosen = choose_audio(audios)
    acodec = chosen["codec"] if chosen else ""
    aidx = chosen["index"] if chosen else None
    fmt = data.get("format") or {}
    container = fmt.get("format_name", "")
    try:
        duration = float(fmt.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    return vcodec, pixfmt, acodec, container, subs, aidx, audios, duration, attachments


def choose_audio(audios: List[dict], want_index: Optional[int] = None) -> Optional[dict]:
    """Pick an audio track: want_index if valid, else Japanese > English > first."""
    if want_index is not None:
        m = next((au for au in audios if au["index"] == want_index), None)
        if m:
            return m
    for pref in (("jpn", "ja"), ("eng", "en")):
        m = next((au for au in audios if au["lang"] in pref), None)
        if m:
            return m
    return audios[0] if audios else None


def audio_label(au: dict, n: int) -> str:
    lang = au.get("lang") or ""
    return au.get("title") or (lang.upper() if lang else f"Audio {n + 1}")


def audio_meta(audios: List[dict], chosen_index: Optional[int]) -> List[dict]:
    """Frontend-facing audio descriptors."""
    return [{"index": au["index"], "label": audio_label(au, n), "lang": au["lang"],
             "default": au["index"] == chosen_index} for n, au in enumerate(audios)]


def is_passthrough(vcodec: str, pix_fmt: str, acodec: str, container: str) -> bool:
    """True if it plays in a <video> tag as-is: MP4 + 8-bit H.264 + AAC."""
    return ("mp4" in (container or "") and vcodec == "h264"
            and pix_fmt in ("yuv420p", "yuvj420p", "") and acodec == "aac")


def vcodec_copyable(vcodec: str, pix_fmt: str) -> bool:
    """True if a browser can decode this codec by stream-copy. H.264 8-bit plays
    everywhere; HEVC plays on Safari/Apple. 10-bit H.264 (Hi10) decodes nowhere."""
    ten_bit = "10" in (pix_fmt or "") and "le" in (pix_fmt or "")
    if vcodec == "h264":
        return not ten_bit
    return vcodec == "hevc"


def build_hls_cmd(ffmpeg: str, url: str, out_dir: str, mode: str,
                  headers: Optional[Dict[str, str]] = None, probed: Optional[Tuple] = None,
                  want_audio: Optional[int] = None,
                  ffprobe: str = "ffprobe", venc: Optional[list] = None,
                  ua: Optional[str] = None) -> Tuple[list, dict]:
    """The single-pass HLS ffmpeg command + a decision info dict. Video copied when
    decodable else re-encoded via ``venc``; audio copied when AAC/MP3 else AAC.
    Subtitles are handled out-of-band, so nothing is muxed here."""
    venc = venc or DEFAULT_VENC
    vcodec, pix_fmt, acodec, container, subs, aidx, audios, _dur, _atts = (
        probed or probe(ffprobe, url, headers, ua=ua))
    chosen = choose_audio(audios, want_audio)
    if chosen:
        aidx, acodec = chosen["index"], chosen["codec"]
    info = {"video": vcodec, "pix_fmt": pix_fmt, "audio": acodec,
            "container": container, "mode": mode}
    amap = f"0:{aidx}" if aidx is not None else "0:a:0?"

    if mode == "transcode":
        vopt = list(venc)
        info["video_action"] = "transcode->h264 (8-bit)"
    elif mode == "remux" or vcodec in ("h264", "hevc"):
        vopt = ["-c:v", "copy"]
        info["video_action"] = f"copy ({vcodec})"
    else:   # auto + unknown codec -> must transcode
        vopt = list(venc)
        info["video_action"] = f"transcode->h264 (source {vcodec})"

    if mode == "transcode" or acodec not in ("aac", "mp3"):
        aopt = ["-c:a", "aac", "-b:a", "192k"]
        info["audio_action"] = f"transcode->aac (source {acodec})"
    else:
        aopt = ["-c:a", "copy"]
        info["audio_action"] = f"copy ({acodec})"

    cmd = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "warning",
        *(["-user_agent", ua] if ua else []),
        *header_args(headers), *_RECONNECT,
        "-i", url,
        "-map", "0:v:0", "-map", amap,
        *vopt, *aopt,
        # MPEG-TS segments (not fMP4): one continuous ffmpeg produces contiguous,
        # non-overlapping segments, so there's no DTS-seam problem to solve, and
        # TS avoids the fMP4 sidx boxes that Chrome's MSE parser rejects. event
        # playlist keeps all segments + writes ENDLIST at the end.
        "-f", "hls", "-hls_time", "6", "-hls_playlist_type", "event",
        "-hls_segment_type", "mpegts", "-hls_flags", "independent_segments",
        "-hls_segment_filename", os.path.join(out_dir, "seg_%04d.ts"),
        os.path.join(out_dir, "index.m3u8"),
    ]
    info["subs_streams"] = subs
    info["audios_raw"] = audios
    info["chosen_audio_index"] = aidx
    return cmd, info


def segment_cmd(ffmpeg: str, aac: bool = True, amap: str = "0:a:0?") -> list:
    """Remux ONE segment fed via stdin (MKV header + cluster bytes) to fMP4.
    fMP4 (not MPEG-TS) is essential: with B-frames, TS segments overlap in DTS at
    keyframe seams and MSE rejects them; fMP4 fragments carry their own timing and
    hls.js places them by the playlist timeline. Video copied; audio copied if
    AAC/MP3 else AAC."""
    acodec = ["-c:a", "copy"] if aac else ["-c:a", "aac", "-b:a", "192k"]
    return [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-i", "pipe:0",
        "-map", "0:v:0", "-map", amap,
        "-c:v", "copy", *acodec,
        "-f", "mp4", "-movflags", "+frag_keyframe+empty_moov+default_base_moof",
        "pipe:1",
    ]


def segment_transcode_cmd(ffmpeg: str, venc: Optional[list] = None, aac: bool = True,
                          amap: str = "0:a:0?") -> list:
    """Like segment_cmd but re-encodes video via ``venc`` (default portable x264),
    for sources the browser can't decode by copy (Hi10, AV1, HDR, or forced). Same
    fMP4 flags, so each segment is independently decodable and Chrome-friendly (no
    sidx). ffmpeg software-decodes the piped window (handles Hi10) and re-encodes."""
    venc = venc or DEFAULT_VENC
    acodec = ["-c:a", "copy"] if aac else ["-c:a", "aac", "-b:a", "192k"]
    return [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-i", "pipe:0",
        "-map", "0:v:0", "-map", amap,
        *venc,
        *acodec,
        "-f", "mp4", "-movflags", "+frag_keyframe+empty_moov+default_base_moof",
        "pipe:1",
    ]


def split_init(buf: bytes) -> Tuple[bytes, bytes]:
    """Split a fragmented MP4 into (init=ftyp+moov, media=rest). One init works for
    all segments since they share track params."""
    m = buf.find(b"moof")
    if m < 4:
        return b"", buf
    return buf[:m - 4], buf[m - 4:]


def seekable_playlist(windows: List[dict]) -> str:
    """Synthesize a complete fMP4 VOD playlist from segment windows. Declaring every
    segment + ENDLIST up front gives the player the full seek bar immediately."""
    lines = ["#EXTM3U", "#EXT-X-VERSION:7",
             f"#EXT-X-TARGETDURATION:{int(max((w['end'] - w['start']) for w in windows)) + 1}",
             "#EXT-X-MEDIA-SEQUENCE:0",
             "#EXT-X-PLAYLIST-TYPE:VOD",
             '#EXT-X-MAP:URI="init.mp4"']
    for i, w in enumerate(windows):
        lines.append(f"#EXTINF:{w['end'] - w['start']:.3f},")
        lines.append(f"seg_{i}.m4s")
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"
