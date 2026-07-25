"""Runtime configuration, sourced from the environment.

Every knob is overridable via an env var so the service can be configured
without code changes (containers, systemd units, etc.). Nothing here holds a
secret by default: credentials for optional plugins live in their own config.
"""
import logging
import os
import shlex
from dataclasses import dataclass, field


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


DEFAULT_VIDEO_ENCODE = "-c:v libx264 -preset veryfast -crf 21 -pix_fmt yuv420p"


# Some CDNs / Cloudflare 403 the default urllib UA as a bot.
DEFAULT_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36")


@dataclass
class Config:
    """Server + engine configuration. ``Config.from_env()`` or construct directly."""

    host: str = "127.0.0.1"
    port: int = 8000

    # override to pin a specific build (hardware encoders, etc.)
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"

    # per-session working dirs (segments, extracted subs/fonts)
    session_root: str = field(
        default_factory=lambda: os.path.join(os.getcwd(), ".remuxd-sessions"))

    # HLS target segment length. Longer = proportionally fewer upstream requests
    # for the same bytes, which is what debrid/CDN rate limiters count; the cost
    # is coarser seek granularity and a slower cold start.
    segment_seconds: int = 10

    # Full video-codec spec for the transcode paths (seek-transcode + singlepass).
    # Default is portable software x264; for Intel Quick Sync use e.g.
    # "-c:v h264_qsv -b:v 6M", for NVENC "-c:v h264_nvenc -b:v 6M", macOS
    # "-c:v h264_videotoolbox -b:v 6M -pix_fmt yuv420p".
    video_encode: str = DEFAULT_VIDEO_ENCODE

    # Read-ahead for seek-copy: keep this many fragments ahead of the playhead
    # warm. Very high ~= download the whole file ahead (bounded by the cache); 0 off.
    prefetch_segments: int = 32
    # Per-session cap on cached fragment bytes. Must cover what read-ahead holds
    # (~ prefetch_segments x segment_seconds x source bitrate), or the cache
    # evicts fragments the prefetcher just paid to fetch and they get
    # re-downloaded. 1GB covers 32 x 10s of a ~25 Mbit/s source.
    fragment_cache_mb: int = 1024
    prefetch_concurrency: int = 4       # global cap on concurrent prefetch ffmpeg jobs

    session_ttl_seconds: int = 30 * 60  # reap after this much idle
    max_sessions: int = 32              # concurrency cap (0 = unlimited)
    # Memoize the expensive per-source prep (resolved URL, probe, size, cues
    # index, header bytes) so a repeat /start on the same src (an audio-track
    # switch, a replay) skips it. Short TTL because CDN links expire (fragments
    # self-heal if one does). 0 disables the cache.
    prep_cache_ttl: int = 120

    user_agent: str = DEFAULT_UA        # UA for upstream fetches
    demo: bool = False                  # serve the browser demo + assets at "/"
    log_level: str = "INFO"
    # If set, every response carries Access-Control-Allow-Origin with this value
    # (e.g. "https://player.example.com" or "*"), so a frontend on another origin
    # can drive the API. Empty = no CORS headers.
    cors_origin: str = ""

    @property
    def fragment_cache_bytes(self) -> int:
        return self.fragment_cache_mb * 1024 * 1024

    @property
    def video_encode_args(self) -> list:
        return shlex.split(self.video_encode)

    @classmethod
    def from_env(cls, **overrides) -> "Config":
        cfg = cls(
            host=os.environ.get("REMUXD_HOST", "127.0.0.1"),
            port=_int("REMUXD_PORT", _int("PORT", 8000)),
            ffmpeg=os.environ.get("FFMPEG_BIN", "ffmpeg"),
            ffprobe=os.environ.get("FFPROBE_BIN", "ffprobe"),
            session_root=os.environ.get(
                "REMUXD_SESSION_ROOT",
                os.path.join(os.getcwd(), ".remuxd-sessions")),
            segment_seconds=_int("REMUXD_SEGMENT_SECONDS", 10),
            video_encode=os.environ.get("REMUXD_VIDEO_ENCODE", DEFAULT_VIDEO_ENCODE),
            prefetch_segments=_int("REMUXD_PREFETCH_SEGMENTS", 32),
            fragment_cache_mb=_int("REMUXD_FRAGMENT_CACHE_MB", 1024),
            prefetch_concurrency=_int("REMUXD_PREFETCH_CONCURRENCY", 4),
            session_ttl_seconds=_int("REMUXD_SESSION_TTL", 30 * 60),
            max_sessions=_int("REMUXD_MAX_SESSIONS", 32),
            prep_cache_ttl=_int("REMUXD_PREP_CACHE_TTL", 120),
            user_agent=os.environ.get("REMUXD_USER_AGENT", DEFAULT_UA),
            demo=os.environ.get("REMUXD_DEMO", "").lower() in ("1", "true", "yes"),
            log_level=os.environ.get("REMUXD_LOG_LEVEL", "INFO").upper(),
            cors_origin=os.environ.get("REMUXD_CORS_ORIGIN", ""),
        )
        for k, v in overrides.items():
            setattr(cfg, k, v)
        try:
            shlex.split(cfg.video_encode)          # fail fast on a malformed spec
        except ValueError:
            logging.getLogger("remuxd.config").warning(
                "invalid REMUXD_VIDEO_ENCODE %r; using default", cfg.video_encode)
            cfg.video_encode = DEFAULT_VIDEO_ENCODE
        return cfg
