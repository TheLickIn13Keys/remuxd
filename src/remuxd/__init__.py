"""remuxd: on-demand remux/transcode of arbitrary video URLs to HLS.

Point it at any http(s) MKV/MP4/WebM and it probes the source, decides whether
the browser can stream-copy the codecs (fast, lossless) or must transcode, then
serves a seekable HLS stream, plus extracted text subtitles and embedded fonts.

Public API (stable):
    from remuxd import Config, Engine, SessionManager
    from remuxd.server import serve

The HTTP contract is documented in remuxd.server.
"""
__version__ = "0.1.0"

from .config import Config
from .engine import Engine
from .session import Session, SessionManager

__all__ = ["Config", "Engine", "Session", "SessionManager", "__version__"]
