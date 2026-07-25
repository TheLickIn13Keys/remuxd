"""HTTP server exposing the remux engine (see README for the endpoint list).

All GET; every stream is scoped by the session id from ``/start``, so many clients
stream at once. ``mode`` = remux (default) | auto | transcode; ``headers`` = a JSON
blob of upstream headers; ``audio`` = an absolute stream index.
"""
import argparse
import json
import logging
import os
import shutil
import signal
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlparse

from . import demo
from . import netio
from .config import Config
from .engine import Engine, StreamError
from .media import seekable_playlist
from .session import CapacityError, SessionManager

log = logging.getLogger("remuxd.server")

_CTYPE_HLS = "application/vnd.apple.mpegurl"


def _guess_ctype(name: str) -> str:
    if name.endswith(".m3u8"):
        return _CTYPE_HLS
    if name.endswith(".ts"):
        return "video/mp2t"
    if name.endswith((".m4s", ".mp4")):
        return "video/mp4"
    return "application/octet-stream"


class Handler(BaseHTTPRequestHandler):
    # injected by serve()
    engine: Engine = None
    config: Config = None
    resolver = None            # callable(anilist_id) -> dict, or None
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):   # route through logging instead of stderr
        log.debug("%s - %s", self.address_string(), fmt % args)

    def send_response(self, code, message=None):
        # tracked so the catch-all error handler knows whether headers already
        # went out (writing a JSON error into a half-sent body corrupts the stream)
        self._response_started = True
        super().send_response(code, message)

    def _cors_headers(self):
        if self.config.cors_origin:
            self.send_header("Access-Control-Allow-Origin", self.config.cors_origin)
            self.send_header("Access-Control-Expose-Headers",
                             "Content-Length, Content-Range, Accept-Ranges")

    def _send(self, code, body, ctype="text/html; charset=utf-8", extra=None,
              cache=None):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if cache:
            self.send_header("Cache-Control", cache)
        self._cors_headers()
        for k, val in (extra or {}).items():
            self.send_header(k, val)
        self.end_headers()
        if self.command == "HEAD":
            return
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, code, obj):
        self._send(code, json.dumps(obj).encode(), "application/json")

    def _err(self, code, msg):
        self._json(code, {"error": msg})

    def _send_ranged(self, data: bytes, ctype: str, cache=None):
        """Serve ``data`` honoring a single Range request (Safari native HLS +
        the demo's growing-subtitle polling both need it)."""
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            try:
                s, _, e = rng[6:].partition("-")
                start = int(s)
                end = int(e) if e else len(data) - 1
                chunk = data[start:end + 1]
                return self._send(206, chunk, ctype, {
                    "Content-Range": f"bytes {start}-{end}/{len(data)}",
                    "Accept-Ranges": "bytes"}, cache=cache)
            except (ValueError, IndexError):
                pass
        self._send(200, data, ctype, {"Accept-Ranges": "bytes"}, cache=cache)

    def do_GET(self):
        self._response_started = False
        u = urlparse(self.path)
        p = u.path
        try:
            if p == "/":
                return self._root()
            # HEAD is routed through do_GET for the read-only endpoints (the demo
            # polls Content-Length on growing .ass files), but must not reach the
            # ones with side effects: a HEAD /start would spin up a whole session.
            if p in ("/start", "/resolve") and self.command == "HEAD":
                return self._err(405, "method not allowed")
            if p == "/start":
                return self._start(parse_qs(u.query))
            if p == "/resolve":
                return self._resolve(parse_qs(u.query))
            if p.startswith("/proxy/"):
                return self._proxy(p[len("/proxy/"):])
            if p.startswith("/hls/"):
                return self._hls(p[len("/hls/"):])
            if p.startswith("/subs/"):
                return self._subs(p[len("/subs/"):])
            if p.startswith("/subwindow/"):
                return self._subwindow(p[len("/subwindow/"):], parse_qs(u.query))
            if p.startswith("/fontlist/"):
                return self._fontlist(p[len("/fontlist/"):])
            if p.startswith("/fonts/"):
                return self._fonts(p[len("/fonts/"):])
            if p.startswith("/static/"):
                return self._static(p[len("/static/"):])
            return self._err(404, "not found")
        except BrokenPipeError:
            pass
        except Exception as e:   # never let a handler crash the connection loop
            if self._response_started:
                # headers already out, so a JSON error would land mid-body; just
                # drop the connection so the client sees a clean failure
                log.exception("handler error for %s", self.path)
                self.close_connection = True
            elif getattr(e, "status", None) in (429, 503):
                # Upstream rate-limited us. A 500 tells the player the segment is
                # broken and many give up on it; 503 + Retry-After is both honest
                # and something players are built to retry. No traceback either:
                # the engine already logged the refusal and its backoff.
                wait = max(1, int(getattr(e, "retry_after", None) or 2))
                log.warning("upstream rate-limited %s; 503 (retry in %ds)",
                            self.path, wait)
                self._send(503, json.dumps({"error": str(e)}).encode(),
                           "application/json", {"Retry-After": str(wait)})
            else:
                log.exception("handler error for %s", self.path)
                self._err(500, str(e))

    def do_HEAD(self):
        # same routing as GET; _send suppresses the body (Content-Length intact)
        self.do_GET()

    def do_POST(self):
        # /stop only. POST because it has side effects, and because
        # navigator.sendBeacon (the one request browsers deliver reliably from a
        # closing tab) sends POST.
        self._response_started = False
        p = urlparse(self.path).path
        try:
            if p.startswith("/stop/"):
                return self._stop(p[len("/stop/"):])
            return self._err(404, "not found")
        except Exception as e:
            log.exception("handler error for %s", self.path)
            if self._response_started:
                self.close_connection = True
            else:
                self._err(500, str(e))

    def do_OPTIONS(self):
        self._response_started = False
        if not self.config.cors_origin:
            return self._err(404, "not found")
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", self.config.cors_origin)
        self.send_header("Access-Control-Allow-Methods", "GET, HEAD, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Range, Content-Type")
        self.send_header("Access-Control-Max-Age", "86400")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _root(self):
        if self.config.demo:
            return self._send(200, demo.PAGE)
        return self._json(200, {"service": "remuxd", "demo": False,
                                "start": "/start?src=<url>"})

    def _start(self, qs):
        # parse_qs already URL-decoded the values once; decoding again would
        # corrupt sources containing literal %xx sequences (signed CDN tokens).
        src = qs.get("src", [""])[0]
        if not src:
            return self._err(400, "no src")
        # http(s) only: anything else (file://, ffmpeg's exotic protocols) would
        # let a client read local files or reach internal services (SSRF).
        if urlparse(src).scheme not in ("http", "https"):
            return self._err(400, "src must be an http(s) URL")
        mode = qs.get("mode", ["auto"])[0]
        headers = None
        hdr_raw = qs.get("headers", [""])[0]
        if hdr_raw:
            try:
                headers = json.loads(hdr_raw)
            except (ValueError, TypeError):
                headers = None
        want_audio = None
        if qs.get("audio", [""])[0]:
            try:
                want_audio = int(qs["audio"][0])
            except ValueError:
                want_audio = None
        try:
            info = self.engine.start(src, mode, headers, want_audio)
            return self._json(200, info)
        except CapacityError as e:
            return self._err(503, str(e))
        except StreamError as e:
            return self._err(502, str(e))
        except Exception as e:
            log.exception("start failed")
            return self._err(500, str(e))

    def _resolve(self, qs):
        if self.resolver is None:
            return self._err(404, "resolver plugin not configured")
        anilist = (qs.get("anilist", [""])[0]).strip()
        if not anilist:
            return self._err(400, "no anilist id")
        try:
            return self._json(200, self.resolver(anilist))
        except Exception as e:
            log.exception("resolve failed")
            return self._err(500, str(e))

    def _stop(self, sid):
        """Tear a session down now (kill prefetch/ffmpeg, drop the cache, remove
        the working dir) instead of leaving it running until the idle TTL. Clients
        should call this when the user switches away from a stream."""
        sess = self.engine.sessions.get(sid)
        if not sess:
            return self._err(404, "no such session")
        self.engine.sessions.remove(sid)
        return self._json(200, {"stopped": sid})

    def _proxy(self, sid):
        sess = self.engine.sessions.get(sid)
        if not sess or not sess.url:
            return self._err(404, "no passthrough session")
        hdrs = dict(sess.headers or {})
        rng = self.headers.get("Range")
        if rng:
            hdrs["Range"] = rng
        try:
            up = netio.open_url(sess.url, self.config.user_agent, hdrs, timeout=60)
        except Exception as e:
            return self._err(502, f"upstream error: {e}")
        try:
            self.send_response(up.status)
            for h in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges"):
                val = up.headers.get(h)
                if val:
                    self.send_header(h, val)
            if not up.headers.get("Accept-Ranges"):
                self.send_header("Accept-Ranges", "bytes")
            self._cors_headers()
            if not up.headers.get("Content-Length"):
                # no length -> we can't frame the body for keep-alive; close so
                # the client sees EOF as end-of-body instead of hanging
                self.send_header("Connection", "close")
                self.close_connection = True
            self.end_headers()
            if self.command == "HEAD":
                return
            shutil.copyfileobj(up, self.wfile, length=256 * 1024)
        except (BrokenPipeError, ConnectionResetError):
            pass   # client seeked/closed; normal during scrubbing
        finally:
            up.close()

    def _hls(self, rel):
        parts = rel.split("/", 1)
        sid = parts[0]
        name = parts[1] if len(parts) > 1 else ""
        sess = self.engine.sessions.get(sid)
        if not sess:
            return self._err(404, "no such session")
        # segments/init are immutable for a given sid, so let the player cache
        # them across back-seeks and replays instead of re-fetching
        immutable = "public, max-age=3600, immutable"
        if sess.on_demand:
            if name == "index.m3u8":
                return self._send(200, seekable_playlist(sess.windows), _CTYPE_HLS,
                                  cache=immutable)   # full VOD playlist, never changes
            if name == "init.mp4":
                init = self.engine.serve_init(sess)
                if init is None:
                    return self._err(502, "init failed")
                return self._send(200, init, "video/mp4", cache=immutable)
            if name.startswith("seg_") and name.endswith(".m4s"):
                try:
                    i = int(name[4:-4])
                except ValueError:
                    return self._err(404, "bad segment")
                if i < 0 or i >= len(sess.windows):
                    return self._err(404, "segment out of range")
                media_bytes = self.engine.serve_segment(sess, i)
                if media_bytes is None:
                    return self._err(502, f"segment {i} failed")
                return self._send(200, media_bytes, "video/mp4", cache=immutable)
            return self._err(404, "not found")
        # singlepass: real files on disk in the session dir
        base = os.path.normpath(sess.dir)   # normpath both sides: "./x" vs "x"
        full = os.path.normpath(os.path.join(base, name))
        # separator-terminated prefix: a bare startswith would admit siblings
        if not full.startswith(base + os.sep) or not os.path.isfile(full):
            return self._err(404, "not found")
        with open(full, "rb") as f:
            data = f.read()
        # the live playlist grows until ffmpeg finishes; segments are immutable
        cache = "no-store" if name.endswith(".m3u8") else immutable
        return self._send_ranged(data, _guess_ctype(name), cache=cache)

    def _subs(self, rel):
        sid, _, n_str = rel.partition("/")
        sess = self.engine.sessions.get(sid)
        try:
            n = int(n_str)
        except ValueError:
            return self._err(404, "bad sub index")
        if not sess or n < 0 or n >= len(sess.tracks):
            return self._err(404, "no subtitles")
        self.engine.ensure_sub_extraction(sess)   # lazy: starts on first demand
        path = sess.tracks[n]["path"]
        if not os.path.isfile(path):
            return self._err(404, "no subtitles")   # not extracted yet
        with open(path, "rb") as f:
            data = f.read()
        # the file grows while extraction runs, so clients must always re-poll
        return self._send_ranged(data, "text/plain; charset=utf-8", cache="no-store")

    def _subwindow(self, sid, qs):
        sess = self.engine.sessions.get(sid)
        if not sess or not sess.on_demand:
            return self._err(404, "no window subs")
        try:
            n = int(qs.get("n", ["0"])[0])
            t = float(qs.get("t", ["0"])[0])
        except ValueError:
            return self._err(400, "bad params")
        out = self.engine.subwindow(sess, n, t)
        if not out:
            return self._err(502, "extract failed")
        return self._send(200, out, "text/plain; charset=utf-8")

    # sfnt/TrueType, OpenType, TrueType Collection, WOFF, WOFF2, legacy Mac TrueType
    _FONT_MAGIC = (b"\x00\x01\x00\x00", b"OTTO", b"ttcf", b"wOFF", b"wOF2", b"true")
    _FONT_CTYPES = {".ttf": "font/ttf", ".otf": "font/otf", ".ttc": "font/collection",
                    ".woff": "font/woff", ".woff2": "font/woff2"}

    @classmethod
    def _is_font_file(cls, path):
        try:
            with open(path, "rb") as f:
                return f.read(4) in cls._FONT_MAGIC
        except OSError:
            return False

    def _fontlist(self, sid):
        sess = self.engine.sessions.get(sid)
        if not sess:
            return self._err(404, "no such session")   # lets stale clients stop polling
        self.engine.ensure_sub_extraction(sess)   # fonts come from the same pass
        fdir = sess.fonts_dir()
        # ffmpeg dumps ALL attachments (cover art, chapter thumbs, ...) with
        # whatever filenames the MKV declares, often extensionless. Sniff magic
        # bytes: only real fonts go to the renderer (a JPEG breaks font loading),
        # and a font named "OpenSans" without .ttf still gets served.
        names = sorted(n for n in os.listdir(fdir)
                       if self._is_font_file(os.path.join(fdir, n))) \
            if os.path.isdir(fdir) else []
        # quote: system-font filenames contain spaces ("Trebuchet MS.ttf");
        # unencoded they never round-trip through the browser's fetch
        return self._json(200, [f"/fonts/{sid}/{quote(n)}" for n in names])

    def _fonts(self, rel):
        sid, _, name = rel.partition("/")
        name = unquote(name)   # traversal-safe: normpath+prefix check below
        sess = self.engine.sessions.get(sid)
        if not sess:
            return self._err(404, "no session")
        base = os.path.normpath(sess.fonts_dir())   # normpath both sides: "./x" vs "x"
        full = os.path.normpath(os.path.join(base, name))
        # separator-terminated prefix: a bare startswith would admit siblings
        if not full.startswith(base + os.sep) or not os.path.isfile(full):
            return self._err(404, "font not found")
        ext = os.path.splitext(name)[1].lower()
        ctype = self._FONT_CTYPES.get(ext, "application/octet-stream")
        with open(full, "rb") as f:
            self._send(200, f.read(), ctype,
                       cache="public, max-age=3600, immutable")

    def _static(self, name):
        if not self.config.demo:
            return self._err(404, "not found")
        body, ctype = demo.asset(name)
        if body is None:
            return self._err(404, "asset not found")
        self._send(200, body, ctype)


def _load_resolver(engine: Engine):
    """Return a resolver callable if the AniList plugin imports + is configured,
    else None. Kept optional so the core service has no extra deps/secrets."""
    try:
        from .plugins import anilist
    except Exception as e:
        log.info("resolver plugin unavailable: %s", e)
        return None
    if not anilist.configured():
        log.info("resolver plugin present but not configured (set AIO_* env vars)")
        return None
    return anilist.resolve_api


def serve(config: Config) -> None:
    """Run the server until interrupted. Creates the session manager + engine,
    wires the demo/resolver, and tears everything down cleanly on SIGINT/SIGTERM."""
    for tool in (config.ffmpeg, config.ffprobe):
        if not shutil.which(tool) and not os.path.isfile(tool):
            sys.exit(f"error: {tool} not found (set FFMPEG_BIN/FFPROBE_BIN)")

    sessions = SessionManager(config.session_root, config.session_ttl_seconds,
                              config.max_sessions)
    engine = Engine(config, sessions)
    Handler.engine = engine
    Handler.config = config
    # staticmethod: a bare function set as a class attr would bind `self` as its
    # first arg when called via the instance.
    resolver = _load_resolver(engine)
    Handler.resolver = staticmethod(resolver) if resolver else None

    srv = ThreadingHTTPServer((config.host, config.port), Handler)
    srv.daemon_threads = True

    def shutdown(*_):
        # keep the handler minimal: just stop the accept loop (can't block on
        # srv.shutdown() from inside the signal handler); real teardown happens
        # after serve_forever returns
        log.info("shutting down")
        threading.Thread(target=srv.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    banner = f"▶  remuxd on http://{config.host}:{config.port}/"
    if config.demo:
        banner += "   (demo UI enabled)"
    if Handler.resolver:
        banner += "   (resolver: on)"
    print(banner + "   (Ctrl-C to stop)")
    try:
        srv.serve_forever()
    finally:
        sessions.shutdown()
        srv.server_close()


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="remuxd",
                                 description="On-demand remux/transcode of video URLs to HLS.")
    ap.add_argument("--host", help="bind address (default 127.0.0.1)")
    ap.add_argument("--port", "-p", type=int, help="port (default 8000)")
    ap.add_argument("--demo", action="store_true", help="serve the browser demo page at /")
    ap.add_argument("--session-root", help="working dir for sessions")
    ap.add_argument("--log-level", help="DEBUG/INFO/WARNING/ERROR (default INFO)")
    args = ap.parse_args(argv)

    cfg = Config.from_env()
    if args.host:
        cfg.host = args.host
    if args.port:
        cfg.port = args.port
    if args.demo:
        cfg.demo = True
    if args.session_root:
        cfg.session_root = args.session_root
    if args.log_level:
        cfg.log_level = args.log_level.upper()

    logging.basicConfig(level=getattr(logging, cfg.log_level, logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    serve(cfg)


if __name__ == "__main__":
    main()
