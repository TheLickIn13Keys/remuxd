"""HTTP helpers for upstream sources.

Sources are often debrid/AIOStreams "playback" URLs that 302 to a temporary CDN
link and re-run a lookup per hit (so we resolve once and reuse the final URL). All
requests carry a browser-like UA because some hosts 403 the stock urllib UA.
"""
import urllib.request
from typing import Dict, Optional


def header_args(headers: Optional[Dict[str, str]]) -> list:
    """dict -> ffmpeg/ffprobe ``-headers`` arg list (CRLF-joined), or []."""
    if not headers:
        return []
    blob = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
    return ["-headers", blob]


def _request(url: str, ua: str, headers: Optional[Dict[str, str]] = None) -> urllib.request.Request:
    req = urllib.request.Request(url)
    req.add_header("User-Agent", ua)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    return req


def resolve_final(url: str, ua: str, headers: Optional[Dict[str, str]] = None):
    """Follow redirects to the real CDN file URL AND capture its size in the same
    request, so repeated hits (probe + ffmpeg + every segment) don't re-trigger the
    debrid lookup (429s) and we skip a separate size probe. Uses GET+Range 0-0
    (many resolvers 404/405 on HEAD); the Content-Range carries the total size.
    Returns (final_url, size_or_None); falls back to (original_url, None) on error."""
    def _try(add_range: bool):
        req = _request(url, ua, headers)
        if add_range:
            req.add_header("Range", "bytes=0-0")
        with urllib.request.urlopen(req, timeout=30) as r:
            size = None
            cr = r.headers.get("Content-Range")     # "bytes 0-0/12345"
            if cr and "/" in cr:
                size = int(cr.rsplit("/", 1)[1])
            elif r.status == 200 and r.headers.get("Content-Length"):
                size = int(r.headers["Content-Length"])   # server ignored Range
            return (r.url or url), size

    try:
        return _try(add_range=True)
    except Exception:
        pass
    try:
        return _try(add_range=False)   # some hosts choke on Range
    except Exception:
        return url, None


def resolve_final_url(url: str, ua: str, headers: Optional[Dict[str, str]] = None) -> str:
    """Just the post-redirect CDN URL (used by re-resolve paths that don't need size)."""
    return resolve_final(url, ua, headers)[0]


def content_length(url: str, ua: str, headers: Optional[Dict[str, str]] = None) -> Optional[int]:
    """Total size of the remote file (bytes) via a Range probe, or None."""
    return resolve_final(url, ua, headers)[1]
