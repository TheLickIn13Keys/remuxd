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
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Dict, Optional, Tuple
from urllib.parse import urljoin, urlsplit

_POOL_MAX_PER_HOST = 8
_MAX_REDIRECTS = 5

_pool_lock = threading.Lock()
_pool: Dict[tuple, list] = {}    # (scheme, host, port) -> idle connections


class UpstreamError(OSError):
    """An upstream fetch failed (bad scheme, HTTP error status, redirect loop).

    ``retry_after`` carries the parsed Retry-After header (seconds) when the host
    sent one, so callers can back off for as long as it asked instead of guessing."""

    def __init__(self, msg: str, status: Optional[int] = None,
                 retry_after: Optional[float] = None):
        super().__init__(msg)
        self.status = status
        self.retry_after = retry_after


def _retry_after(resp) -> Optional[float]:
    """Retry-After as seconds from now, for both the delta and HTTP-date forms."""
    raw = (resp.getheader("Retry-After") or "").strip()
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


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
    the connection instead (a half-read keep-alive socket can't be reused).

    ``skip``/``limit`` trim the body down to the range the caller asked for, for
    servers that answer a ranged GET with more than was requested."""

    def __init__(self, conn, key, resp, url, skip: int = 0,
                 limit: Optional[int] = None):
        self._conn = conn
        self._key = key
        self._resp = resp
        self._done = False
        self._skip = skip
        self._limit = limit
        self._served = 0
        self.url = url
        self.status = resp.status
        self.headers = resp.headers

    def read(self, amt: Optional[int] = None) -> bytes:
        if self._done:
            return b""
        while self._skip > 0:
            chunk = self._raw(min(self._skip, 1 << 20))
            if not chunk:
                return b""
            self._skip -= len(chunk)
        if self._limit is not None:
            left = self._limit - self._served
            if left <= 0:
                self._finish(reusable=False)
                return b""
            amt = left if amt is None else min(amt, left)
        data = self._raw(amt)
        self._served += len(data)
        # the caller has its whole range; the rest of the body is dead weight, and
        # leaving it unread makes the socket unreusable anyway
        if self._limit is not None and self._served >= self._limit:
            self._finish(reusable=False)
        return data

    def _raw(self, amt: Optional[int]) -> bytes:
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


def _content_range_start(resp) -> Optional[int]:
    """First byte position from a ``Content-Range: bytes 123-456/789`` header."""
    raw = (resp.getheader("Content-Range") or "").strip()
    if not raw.startswith("bytes "):
        return None
    try:
        return int(raw[6:].split("/", 1)[0].split("-", 1)[0].strip())
    except ValueError:
        return None


def _range_trim(resp, url: str, rng: Tuple[int, Optional[int]]) -> Tuple[int, Optional[int]]:
    """(skip, limit) that turn this response's body into exactly bytes [start,end).

    A host may answer a ranged GET with the whole file (200) or with a range
    starting elsewhere. Nothing downstream re-checks: the bytes are piped straight
    into ffmpeg behind an MKV header, so a body that silently begins at 0 arrives
    as a second EBML header and demuxes as garbage.

    A 200 to a non-zero range is an error rather than a skip-forward: swallowing
    everything before the offset would download the whole file, and a host that
    can't serve ranges can't serve the per-segment path at all."""
    start, end = rng
    got = _content_range_start(resp) if resp.status == 206 else 0
    if got is None:
        got = start                      # 206 without a parseable Content-Range
    if got > start:
        raise UpstreamError(f"range start {got} past requested {start} for {url}",
                            status=resp.status)
    skip = start - got
    if skip and resp.status != 206:
        raise UpstreamError(f"HTTP {resp.status}: range {start}- ignored for {url}",
                            status=resp.status)
    limit = None if end is None else max(0, end - start)
    return skip, limit


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
            ra = _retry_after(resp)
            conn.close()
            raise UpstreamError(f"HTTP {resp.status} for {url}", status=resp.status,
                                retry_after=ra)
        if rng is None:
            return PooledResponse(conn, key, resp, url)
        try:
            skip, limit = _range_trim(resp, url, rng)
        except UpstreamError:
            conn.close()
            raise
        return PooledResponse(conn, key, resp, url, skip=skip, limit=limit)
    raise UpstreamError(f"too many redirects for {url}")


def fetch_range(url: str, start: int, end: Optional[int], ua: str,
                headers: Optional[Dict[str, str]] = None,
                timeout: float = 60) -> bytes:
    """Fetch bytes [start, end) (end=None => to EOF) over a pooled connection.

    Reads to exhaustion rather than trusting one read(): callers parse the result
    as a structure (the MKV header, a Cues index), where a short read isn't a
    small result but a malformed one."""
    with open_url(url, ua, headers, rng=(start, end), timeout=timeout) as r:
        parts = []
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                return b"".join(parts)
            parts.append(chunk)


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
