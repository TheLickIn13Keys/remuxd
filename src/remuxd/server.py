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
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from . import demo
from .config import Config
from .engine import Engine, StreamError
from .media import seekable_playlist
from .session import SessionManager

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

    def _send(self, code, body, ctype="text/html; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, val in (extra or {}).items():
            self.send_header(k, val)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, code, obj):
        self._send(code, json.dumps(obj).encode(), "application/json")

    def _err(self, code, msg):
        self._json(code, {"error": msg})

    def _send_ranged(self, data: bytes, ctype: str):
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
                    "Accept-Ranges": "bytes"})
            except (ValueError, IndexError):
                pass
        self._send(200, data, ctype, {"Accept-Ranges": "bytes"})

    def do_GET(self):
        u = urlparse(self.path)
        p = u.path
        try:
            if p == "/":
                return self._root()
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
            log.exception("handler error for %s", self.path)
            self._err(500, str(e))

    def _root(self):
        if self.config.demo:
            return self._send(200, demo.PAGE)
        return self._json(200, {"service": "remuxd", "demo": False,
                                "start": "/start?src=<url>"})

    def _start(self, qs):
        src = unquote(qs.get("src", [""])[0])
        if not src:
            return self._err(400, "no src")
        mode = qs.get("mode", ["auto"])[0]
        headers = None
        hdr_raw = qs.get("headers", [""])[0]
        if hdr_raw:
            try:
                headers = json.loads(unquote(hdr_raw))
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

    def _proxy(self, sid):
        sess = self.engine.sessions.get(sid)
        if not sess or not sess.url:
            return self._err(404, "no passthrough session")
        req = urllib.request.Request(sess.url)
        req.add_header("User-Agent", self.config.user_agent)
        for k, v in (sess.headers or {}).items():
            req.add_header(k, v)
        rng = self.headers.get("Range")
        if rng:
            req.add_header("Range", rng)
        try:
            up = urllib.request.urlopen(req, timeout=60)
        except Exception as e:
            return self._err(502, f"upstream error: {e}")
        self.send_response(up.status)
        for h in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges"):
            val = up.headers.get(h)
            if val:
                self.send_header(h, val)
        if not up.headers.get("Accept-Ranges"):
            self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        try:
            shutil.copyfileobj(up, self.wfile, length=256 * 1024)
        except (BrokenPipeError, ConnectionResetError):
            pass   # client seeked/closed — normal during scrubbing

    def _hls(self, rel):
        parts = rel.split("/", 1)
        sid = parts[0]
        name = parts[1] if len(parts) > 1 else ""
        sess = self.engine.sessions.get(sid)
        if not sess:
            return self._err(404, "no such session")
        if sess.on_demand:
            if name == "index.m3u8":
                return self._send(200, seekable_playlist(sess.windows), _CTYPE_HLS)
            if name == "init.mp4":
                init = self.engine.serve_init(sess)
                if init is None:
                    return self._err(502, "init failed")
                return self._send(200, init, "video/mp4")
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
                return self._send(200, media_bytes, "video/mp4")
            return self._err(404, "not found")
        # singlepass: real files on disk in the session dir
        safe = os.path.normpath(name).lstrip("./")
        full = os.path.join(sess.dir, safe)
        if not full.startswith(sess.dir) or not os.path.isfile(full):
            return self._err(404, "not found")
        with open(full, "rb") as f:
            data = f.read()
        return self._send_ranged(data, _guess_ctype(name))

    def _subs(self, rel):
        sid, _, n_str = rel.partition("/")
        sess = self.engine.sessions.get(sid)
        try:
            n = int(n_str)
        except ValueError:
            return self._err(404, "bad sub index")
        if not sess or n < 0 or n >= len(sess.tracks):
            return self._err(404, "no subtitles")
        path = sess.tracks[n]["path"]
        if not os.path.isfile(path):
            return self._err(404, "no subtitles")   # not extracted yet
        with open(path, "rb") as f:
            data = f.read()
        return self._send_ranged(data, "text/plain; charset=utf-8")

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

    def _fontlist(self, sid):
        sess = self.engine.sessions.get(sid)
        if not sess:
            return self._err(404, "no such session")   # lets stale clients stop polling
        fdir = sess.fonts_dir()
        names = sorted(os.listdir(fdir)) if os.path.isdir(fdir) else []
        return self._json(200, [f"/fonts/{sid}/{n}" for n in names])

    def _fonts(self, rel):
        sid, _, name = rel.partition("/")
        sess = self.engine.sessions.get(sid)
        if not sess:
            return self._err(404, "no session")
        base = sess.fonts_dir()
        full = os.path.normpath(os.path.join(base, name))
        if not full.startswith(base) or not os.path.isfile(full):
            return self._err(404, "font not found")
        with open(full, "rb") as f:
            self._send(200, f.read(), "font/otf")

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
        log.info("shutting down")
        sessions.shutdown()
        # can't block on srv.shutdown() from inside the signal handler
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
