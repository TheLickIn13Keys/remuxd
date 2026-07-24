"""HTTP helpers for upstream sources.

Sources are often debrid/AIOStreams "playback" URLs that 302 to a temporary CDN
link and re-run a lookup per hit (so we resolve once and reuse the final URL). All
requests carry a browser-like UA because some hosts 403 the stock urllib UA.

Connections are pooled per host with keep-alive: fragment serving makes a byte-range
request every few seconds against the same CDN, and a fresh TCP+TLS handshake per
fetch is often a large share of the fetch time.
"""
import http.client
import threading
from typing import Dict, Optional, Tuple
from urllib.parse import urljoin, urlsplit

_POOL_MAX_PER_HOST = 8
_MAX_REDIRECTS = 5

_pool_lock = threading.Lock()
_pool: Dict[tuple, list] = {}    # (scheme, host, port) -> idle connections


class UpstreamError(OSError):
    """An upstream fetch failed (bad scheme, HTTP error status, redirect loop)."""

    def __init__(self, msg: str, status: Optional[int] = None):
        super().__init__(msg)
        self.status = status


def header_args(headers: Optional[Dict[str, str]]) -> list:
    """dict -> ffmpeg/ffprobe ``-headers`` arg list (CRLF-joined), or []."""
    if not headers:
        return []
    blob = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
    return ["-headers", blob]


def _checkout(scheme: str, host: str, port: Optional[int], timeout: float):
    """-> (connection, pool_key, was_reused)."""
    port = port or (443 if scheme == "https" else 80)
    key = (scheme, host, port)
    with _pool_lock:
        idle = _pool.get(key) or []
        while idle:
            c = idle.pop()
            if c.sock is not None:
                return c, key, True
            c.close()          # dropped by the peer since check-in
    cls = http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
    return cls(host, port, timeout=timeout), key, False


def _checkin(key: tuple, conn) -> None:
    with _pool_lock:
        idle = _pool.setdefault(key, [])
        if len(idle) < _POOL_MAX_PER_HOST:
            idle.append(conn)
            return
    conn.close()


class PooledResponse:
    """File-like read wrapper over an http.client response. When the body has been
    fully read the connection goes back to the pool; ``close()`` before EOF drops
    the connection instead (a half-read keep-alive socket can't be reused)."""

    def __init__(self, conn, key, resp, url):
        self._conn = conn
        self._key = key
        self._resp = resp
        self._done = False
        self.url = url
        self.status = resp.status
        self.headers = resp.headers

    def read(self, amt: Optional[int] = None) -> bytes:
        if self._done:
            return b""
        try:
            data = self._resp.read(amt)
        except Exception:
            self._finish(reusable=False)
            raise
        if self._resp.isclosed():
            self._finish(reusable=not self._resp.will_close)
        return data

    def _finish(self, reusable: bool) -> None:
        if self._done:
            return
        self._done = True
        if reusable and self._conn.sock is not None:
            _checkin(self._key, self._conn)
        else:
            self._conn.close()

    def close(self) -> None:
        self._finish(reusable=False)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def open_url(url: str, ua: str, headers: Optional[Dict[str, str]] = None,
             rng: Optional[Tuple[int, Optional[int]]] = None,
             timeout: float = 30) -> PooledResponse:
    """Pooled GET with keep-alive and redirect following. ``rng=(start, end)``
    requests bytes [start, end) (end=None => to EOF). Raises UpstreamError on a
    non-success status; a stale pooled connection is retried once on a fresh one."""
    for _ in range(_MAX_REDIRECTS + 1):
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise UpstreamError(f"unsupported URL: {url!r}")
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        hdrs = {"User-Agent": ua, **(headers or {})}
        if rng is not None:
            start, end = rng
            hdrs["Range"] = f"bytes={start}-" + ("" if end is None else str(end - 1))

        resp = None
        for attempt in (0, 1):
            conn, key, reused = _checkout(parts.scheme, parts.hostname, parts.port,
                                          timeout)
            try:
                conn.request("GET", path, headers=hdrs)
                resp = conn.getresponse()
                break
            except Exception:
                conn.close()
                if not (reused and attempt == 0):   # fresh conn failed -> real error
                    raise
        if resp.status in (301, 302, 303, 307, 308) and resp.getheader("Location"):
            loc = resp.getheader("Location")
            PooledResponse(conn, key, resp, url).read()     # drain + recycle
            url = urljoin(url, loc)
            continue
        if resp.status >= 400:
            conn.close()
            raise UpstreamError(f"HTTP {resp.status} for {url}", status=resp.status)
        return PooledResponse(conn, key, resp, url)
    raise UpstreamError(f"too many redirects for {url}")


def fetch_range(url: str, start: int, end: Optional[int], ua: str,
                headers: Optional[Dict[str, str]] = None,
                timeout: float = 60) -> bytes:
    """Fetch bytes [start, end) (end=None => to EOF) over a pooled connection."""
    with open_url(url, ua, headers, rng=(start, end), timeout=timeout) as r:
        return r.read()


def resolve_final(url: str, ua: str, headers: Optional[Dict[str, str]] = None):
    """Follow redirects to the real CDN file URL AND capture its size in the same
    request, so repeated hits (probe + ffmpeg + every segment) don't re-trigger the
    debrid lookup (429s) and we skip a separate size probe. Uses GET+Range 0-0
    (many resolvers 404/405 on HEAD); the Content-Range carries the total size.
    Returns (final_url, size_or_None); falls back to (original_url, None) on error."""
    def _try(add_range: bool):
        r = open_url(url, ua, headers, rng=(0, 1) if add_range else None)
        try:
            size = None
            cr = r.headers.get("Content-Range")     # "bytes 0-0/12345"
            if cr and "/" in cr:
                total = cr.rsplit("/", 1)[1]
                if total != "*":
                    size = int(total)
            elif r.status == 200 and r.headers.get("Content-Length"):
                size = int(r.headers["Content-Length"])   # server ignored Range
            if r.status == 206:
                r.read()        # 1 byte; drain so the connection is reusable
            return r.url, size
        finally:
            r.close()

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
