"""Smoke + unit tests for remuxd (stdlib only, no network)."""
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from remuxd import media, Config, SessionManager, Engine   # noqa: E402


def test_decisions():
    assert media.is_passthrough("h264", "yuv420p", "aac", "mov,mp4,m4a") is True
    assert media.is_passthrough("hevc", "yuv420p10le", "aac", "matroska") is False
    assert media.vcodec_copyable("h264", "yuv420p") is True
    assert media.vcodec_copyable("h264", "yuv420p10le") is False   # Hi10
    assert media.vcodec_copyable("hevc", "yuv420p10le") is True
    assert media.vcodec_copyable("av1", "yuv420p") is False


def test_choose_audio():
    audios = [{"index": 1, "lang": "eng", "codec": "aac", "title": ""},
              {"index": 2, "lang": "jpn", "codec": "opus", "title": ""}]
    assert media.choose_audio(audios)["index"] == 2          # jpn preferred
    assert media.choose_audio(audios, want_index=1)["index"] == 1
    assert media.choose_audio([]) is None


def test_seekable_playlist():
    wins = [{"start": 0, "end": 6}, {"start": 6, "end": 10.5}]
    pl = media.seekable_playlist(wins)
    assert "#EXT-X-PLAYLIST-TYPE:VOD" in pl
    assert pl.count("seg_") == 2
    assert pl.strip().endswith("#EXT-X-ENDLIST")


def test_split_init():
    buf = b"ftypxxxxmoovyyyy" + b"\x00\x00\x00\x10moofzzzz"
    init, medi = media.split_init(buf)
    assert init.endswith(b"yyyy")
    assert medi.startswith(b"\x00\x00\x00\x10moof")


def test_session_manager_lifecycle(tmp_root="./.remuxd-test-sessions"):
    # min_evict_idle=0 -> every session is evictable, i.e. plain LRU at the cap
    sm = SessionManager(tmp_root, ttl_seconds=1, max_sessions=2, min_evict_idle=0.0)
    try:
        a = sm.create("seek")
        b = sm.create("seek")
        assert sm.get(a.sid) is a
        assert os.path.isdir(a.dir)
        # cap=2 -> creating a third evicts the LRU (a, since b was touched later)
        sm.get(b.sid)
        c = sm.create("seek")
        assert sm.get(a.sid) is None      # evicted
        assert not os.path.isdir(a.dir)   # dir cleaned
        assert sm.get(b.sid) is b
        assert sm.get(c.sid) is c
        # TTL reap (ttl=1s; reaper wakes ~1s, so allow generous margin)
        time.sleep(3.5)
        assert sm.get(b.sid) is None
        assert sm.get(c.sid) is None
    finally:
        sm.shutdown()
        assert not os.path.isdir(tmp_root)


def test_capacity_rejects_when_all_active(tmp_root="./.remuxd-test-cap"):
    """At the cap with every session recently active, create() must refuse (503
    at the API) instead of killing someone's live stream."""
    from remuxd.session import CapacityError
    sm = SessionManager(tmp_root, ttl_seconds=60, max_sessions=2)  # grace 60s
    try:
        a = sm.create("seek")
        sm.create("seek")
        try:
            sm.create("seek")
            assert False, "expected CapacityError"
        except CapacityError:
            pass
        assert sm.get(a.sid) is a          # nobody was evicted
    finally:
        sm.shutdown()


def test_shutdown_preserves_foreign_root(tmp_root="./.remuxd-test-foreign"):
    """shutdown() must only clean up its own session dirs — the root may be a
    pre-existing directory (REMUXD_SESSION_ROOT=/tmp must not be wiped)."""
    os.makedirs(tmp_root, exist_ok=True)
    keep = os.path.join(tmp_root, "keep.txt")
    with open(keep, "w") as f:
        f.write("precious")
    sm = SessionManager(tmp_root, ttl_seconds=60, max_sessions=4)
    s = sm.create("seek")
    sm.shutdown()
    try:
        assert not os.path.isdir(s.dir)        # session dir cleaned
        assert os.path.isfile(keep)            # foreign file untouched
    finally:
        os.remove(keep)
        os.rmdir(tmp_root)


def test_info_urls_are_sid_scoped():
    """Engine._info must scope every client URL by the session id."""
    cfg = Config(session_root="./.remuxd-test-info")
    sm = SessionManager(cfg.session_root, ttl_seconds=5, max_sessions=4)
    try:
        eng = Engine(cfg, sm)
        sess = sm.create("seek")
        sess.tracks = [{"index": 0, "path": "/x", "lang": "eng",
                        "label": "English", "default": True}]
        info = eng._info(sess, "hevc", "yuv420p10le", "aac", "matroska", "seek-copy",
                         "copy (hevc)", "copy (aac)",
                         playlist=f"/hls/{sess.sid}/index.m3u8",
                         tracks=sess.tracks, audio_meta=[], seekable=True)
        assert info["sid"] == sess.sid
        assert info["playlist"] == f"/hls/{sess.sid}/index.m3u8"
        assert info["subs"] == f"/subs/{sess.sid}"
        assert info["fontlist"] == f"/fontlist/{sess.sid}"
        assert info["subwindow"] == f"/subwindow/{sess.sid}"
        assert info["tracks"][0]["url"] == f"/subs/{sess.sid}/0"
    finally:
        sm.shutdown()


def test_server_endpoints_live():
    """Boot a real server on an ephemeral port and hit the cheap endpoints."""
    import threading
    from http.server import ThreadingHTTPServer
    from remuxd.server import Handler

    cfg = Config(port=0, session_root="./.remuxd-test-srv", demo=True)
    sm = SessionManager(cfg.session_root, ttl_seconds=60, max_sessions=4)
    Handler.engine = Engine(cfg, sm)
    Handler.config = cfg
    Handler.resolver = None
    srv = ThreadingHTTPServer((cfg.host, 0), Handler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{port}"

    def get(path):
        try:
            with urllib.request.urlopen(base + path, timeout=5) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    try:
        st, body = get("/")
        assert st == 200 and b"remuxd" in body                    # demo page
        st, _ = get("/start")                                     # no src
        assert st == 400
        st, _ = get("/start?src=file:///etc/hosts")               # SSRF/local read
        assert st == 400
        st, _ = get("/start?src=ftp%3A%2F%2Fx%2Fy")               # non-http scheme
        assert st == 400
        st, _ = get("/resolve?anilist=1")                         # no plugin
        assert st == 404
        st, _ = get("/hls/deadbeef/index.m3u8")                   # unknown session
        assert st == 404
        st, _ = get("/fontlist/deadbeef")                         # unknown -> 404 (stops stale polls)
        assert st == 404
        st, body = get("/static/jassub.umd.js")                   # demo asset
        assert st == 200 and len(body) > 1000
    finally:
        srv.shutdown()
        sm.shutdown()


def test_resolver_is_not_self_bound():
    """A resolver set on the Handler must be called with only the id — regression
    for a bare function binding `self` as its first positional arg."""
    import threading
    from http.server import ThreadingHTTPServer
    from remuxd.server import Handler

    cfg = Config(port=0, session_root="./.remuxd-test-resolver")
    sm = SessionManager(cfg.session_root, ttl_seconds=60, max_sessions=4)
    Handler.engine = Engine(cfg, sm)
    Handler.config = cfg
    Handler.resolver = staticmethod(lambda anilist: {"media_id": anilist, "results": []})
    srv = ThreadingHTTPServer((cfg.host, 0), Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/resolve?anilist=42",
                                    timeout=5) as r:
            body = r.read()
            assert r.status == 200
            assert b'"media_id": "42"' in body
    finally:
        srv.shutdown()
        sm.shutdown()
        Handler.resolver = None


def test_fragment_cache_evicts_farthest():
    from remuxd.session import FragmentCache
    c = FragmentCache(budget_bytes=300)
    for i in range(5):
        c.put(i, b"x" * 100, playhead=4)     # 100 bytes each, playhead near the end
    assert c.bytes <= 300
    assert c.has(4) and c.has(3) and c.has(2)
    assert not c.has(0) and not c.has(1)      # farthest-behind evicted first


def test_fragment_cache_inflight_dedup():
    """A second claimer of an in-flight index must be told to wait (not produce),
    and be released by put/release — this is what stops the double CDN fetch."""
    from remuxd.session import FragmentCache
    c = FragmentCache(budget_bytes=10_000)
    should, ev = c.claim(9)
    assert should is True and ev is not None          # first claimer produces
    should2, ev2 = c.claim(9)
    assert should2 is False and ev2 is ev             # second waits on same event
    assert not ev.is_set()
    c.put(9, b"y" * 10, playhead=9)
    assert ev.is_set()                                # put wakes waiters
    assert c.claim(9) == (False, None)                # now cached
    # release also wakes waiters (producer failed path)
    should, ev = c.claim(5)
    assert should is True
    c.release(5)
    assert ev.is_set() and not c.has(5)


def test_prefetch_worker_warms_ahead():
    import threading
    from remuxd.session import FragmentCache, Session
    from remuxd.engine import PrefetchWorker

    class FakeEngine:
        def __init__(self):
            self._prefetch_sem = threading.Semaphore(4)
            self.produced = []

        def fragment(self, sess, i):
            self.produced.append(i)
            return (b"init" if i == 0 else b""), b"m" * 10, None

    sess = Session("pf-test", "seek", "./.pf-test-nodir")   # dir never touched
    sess.windows = [{"start": k * 6, "end": k * 6 + 6, "boff": 0, "bend": 10}
                    for k in range(20)]
    sess.cache = FragmentCache(64 * 1024)
    eng = FakeEngine()
    w = PrefetchWorker(eng, sess, read_ahead=5)
    sess.prefetch = w
    w.start()
    try:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and \
                sum(sess.cache.has(i) for i in range(20)) < 6:
            time.sleep(0.05)
        assert sum(sess.cache.has(i) for i in range(20)) >= 6   # window from playhead 0
        assert sess.init == b"init"                             # init cached from seg 0
        w.notify(12)                                            # advance playhead
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not sess.cache.has(14):
            time.sleep(0.05)
        assert sess.cache.has(13) or sess.cache.has(14)         # warmed ahead of new playhead
    finally:
        w.stop()


def test_prefetch_refocuses_on_seek():
    """A big forward or backward seek must re-point read-ahead at the new
    position, not keep warming the pre-seek window."""
    import threading
    from remuxd.session import FragmentCache, Session
    from remuxd.engine import PrefetchWorker

    class SlowEngine:
        """Each fragment takes a beat, so a stale window can't drain instantly."""
        def __init__(self):
            self._prefetch_sem = threading.Semaphore(2)

        def fragment(self, sess, i):
            time.sleep(0.02)
            return b"", b"m" * 10, None

    sess = Session("seek-test", "seek", "./.seek-test-nodir")
    sess.windows = [{"start": k * 6, "end": k * 6 + 6, "boff": 0, "bend": 10}
                    for k in range(200)]
    sess.cache = FragmentCache(64 * 1024)
    w = PrefetchWorker(SlowEngine(), sess, read_ahead=10)
    sess.prefetch = w
    w.start()
    try:
        def wait_for(pred, secs=3):
            end = time.monotonic() + secs
            while time.monotonic() < end and not pred():
                time.sleep(0.02)
            return pred()

        assert wait_for(lambda: sess.cache.has(5))          # warmed near start
        w.notify(120)                                        # big forward seek
        assert wait_for(lambda: sess.cache.has(122)), "did not warm ahead of forward seek"
        w.notify(40)                                         # backward seek
        assert wait_for(lambda: sess.cache.has(42)), "did not warm ahead of backward seek"
    finally:
        w.stop()


def test_segment_commands():
    """Copy vs transcode segment commands: both fMP4 with the Chrome-friendly
    flags (no dash/sidx), differing only in the video codec."""
    copy = media.segment_cmd("ffmpeg", aac=True, amap="0:1")
    tx = media.segment_transcode_cmd("ffmpeg", aac=True, amap="0:1")
    for cmd in (copy, tx):
        assert "+frag_keyframe+empty_moov+default_base_moof" in cmd
        assert "-hls_segment_type" not in cmd and "dash" not in cmd   # no sidx source
        assert cmd[-1] == "pipe:1"
    assert "copy" in copy[copy.index("-c:v") + 1]
    assert copy[copy.index("-c:a") + 1] == "copy"                     # aac=True copies audio
    assert tx[tx.index("-c:v") + 1] == "libx264"                      # portable default
    # custom encoder spec is honored (e.g. Intel Quick Sync)
    qsv = media.segment_transcode_cmd("ffmpeg", venc=["-c:v", "h264_qsv", "-b:v", "6M"])
    assert qsv[qsv.index("-c:v") + 1] == "h264_qsv"


def test_on_demand_property():
    from remuxd.session import Session
    seek = Session("a", "seek", "./.x"); seek.windows = [{}]
    txn = Session("b", "seek-transcode", "./.x"); txn.windows = [{}]
    live = Session("c", "singlepass", "./.x")                         # windows stays None
    assert seek.on_demand and txn.on_demand
    assert not live.on_demand


def test_prep_cache():
    """Per-source prep memo: hit, TTL expiry, and ttl=0 disable."""
    from remuxd import Config, SessionManager
    from remuxd.engine import Engine, _PrepEntry
    cfg = Config(session_root="./.prep-test", prep_cache_ttl=1)
    sm = SessionManager(cfg.session_root, 60, 4)
    try:
        eng = Engine(cfg, sm)
        key = ("srcA", ())
        assert eng._prep_get(key) is None
        e = _PrepEntry("http://cdn/x", 123, ("h264",), time.monotonic())
        eng._prep_put(key, e)
        assert eng._prep_get(key) is e                 # hit
        time.sleep(1.2)
        assert eng._prep_get(key) is None              # expired
        cfg.prep_cache_ttl = 0                          # disabled -> never caches
        eng._prep_put(key, e)
        assert eng._prep_get(key) is None
    finally:
        sm.shutdown()


def test_resolver_cache():
    """resolve_api memoizes by token: second call within TTL skips the search."""
    import os
    from remuxd.plugins import anilist
    os.environ["AIO_CACHE_TTL"] = "60"
    anilist._cache.clear()
    calls = {"n": 0}
    orig = anilist._resolve_streams
    anilist._resolve_streams = lambda aid, s, e, tl=None: (calls.__setitem__("n", calls["n"] + 1)
                                                           or ("tt1:1:1", []))
    try:
        r1 = anilist.resolve_api("154587")
        r2 = anilist.resolve_api("154587")
        assert calls["n"] == 1                    # second served from cache
        assert r1 == r2 and r1["media_id"] == "tt1:1:1"
        os.environ["AIO_CACHE_TTL"] = "0"          # disabled -> always recomputes
        anilist.resolve_api("154587")
        assert calls["n"] == 2
    finally:
        anilist._resolve_streams = orig
        anilist._cache.clear()
        os.environ.pop("AIO_CACHE_TTL", None)


def test_fragment_cache_keeps_readahead():
    """Under budget pressure, evict already-watched back-buffer, not the freshly
    prefetched leading edge (regression for evict/refetch thrash)."""
    from remuxd.session import FragmentCache
    c = FragmentCache(budget_bytes=300)          # 3 x 100 bytes
    c.put(90, b"x" * 100, playhead=100)          # behind
    c.put(110, b"x" * 100, playhead=100)         # ahead
    c.put(120, b"x" * 100, playhead=100)         # ahead; cache full
    c.put(130, b"x" * 100, playhead=100)         # overflow
    assert not c.has(90)                          # behind evicted
    assert c.has(110) and c.has(120) and c.has(130)   # read-ahead kept


def test_resolve_final_size_when_range_ignored():
    """A server that ignores Range 0-0 (200 + full Content-Length, no Content-Range)
    must still yield the size (regression)."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from remuxd.netio import resolve_final

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            body = b"x" * 1000                     # ignores Range: full 200 + body
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        _url, size = resolve_final(f"http://127.0.0.1:{port}/x", "UA")
        assert size == 1000
    finally:
        srv.shutdown()


def test_video_encode_validation():
    """A malformed REMUXD_VIDEO_ENCODE falls back to the default at load, not at
    first transcode."""
    import os
    from remuxd import Config
    from remuxd.config import DEFAULT_VIDEO_ENCODE
    os.environ["REMUXD_VIDEO_ENCODE"] = '-c:v libx264 "unbalanced'
    try:
        cfg = Config.from_env()
        assert cfg.video_encode == DEFAULT_VIDEO_ENCODE
        cfg.video_encode_args      # must not raise
    finally:
        os.environ.pop("REMUXD_VIDEO_ENCODE", None)


def test_stop_endpoint():
    """POST /stop/<sid> must tear the session down immediately — abandoned
    sessions otherwise keep prefetching until the idle TTL. POST-only: it has
    side effects, and navigator.sendBeacon (tab close) sends POST."""
    import threading
    from http.server import ThreadingHTTPServer
    from remuxd.server import Handler

    cfg = Config(port=0, session_root="./.remuxd-test-stop")
    sm = SessionManager(cfg.session_root, ttl_seconds=60, max_sessions=4)
    Handler.engine = Engine(cfg, sm)
    Handler.config = cfg
    Handler.resolver = None
    srv = ThreadingHTTPServer((cfg.host, 0), Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"

    def req(path, post=False):
        r = urllib.request.Request(base + path, data=b"" if post else None)
        try:
            with urllib.request.urlopen(r, timeout=5) as resp:
                return resp.status
        except urllib.error.HTTPError as e:
            return e.code

    try:
        a = sm.create("seek")
        b = sm.create("seek")
        assert req(f"/stop/{a.sid}", post=True) == 200
        assert sm.get(a.sid) is None
        assert not os.path.isdir(a.dir)                  # working dir cleaned
        assert req(f"/stop/{b.sid}") == 404              # GET must not mutate
        assert sm.get(b.sid) is not None
        assert req("/stop/nosuch", post=True) == 404
        assert req("/hls/x/init.mp4", post=True) == 404  # POST only routes /stop
    finally:
        srv.shutdown()
        sm.shutdown()


def test_fontlist_serves_only_fonts():
    """ffmpeg dumps ALL attachments (cover art included) under whatever names the
    MKV declares; /fontlist must expose exactly the real fonts (by magic bytes,
    not extension — attachments are often extensionless or mislabeled), and
    /fonts must send a sensible content-type."""
    import json as _json
    import threading
    from http.server import ThreadingHTTPServer
    from remuxd.server import Handler

    cfg = Config(port=0, session_root="./.remuxd-test-fonts")
    sm = SessionManager(cfg.session_root, ttl_seconds=60, max_sessions=4)
    Handler.engine = Engine(cfg, sm)
    Handler.config = cfg
    Handler.resolver = None
    srv = ThreadingHTTPServer((cfg.host, 0), Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"

    files = {
        "OpenSans.TTF": b"\x00\x01\x00\x00rest",   # sfnt/TrueType
        "signs.otf": b"OTTOrest",                  # OpenType
        "NotoSansJP": b"\x00\x01\x00\x00rest",     # real font, NO extension
        "fake.ttf": b"not a font at all",          # font extension, not a font
        "cover.jpg": b"\xff\xd8\xff\xe0jfif",
        "notes.txt": b"hello",
    }
    try:
        sess = sm.create("seek")
        fdir = sess.fonts_dir()
        os.makedirs(fdir)
        for n, data in files.items():
            with open(os.path.join(fdir, n), "wb") as f:
                f.write(data)
        with urllib.request.urlopen(f"{base}/fontlist/{sess.sid}", timeout=5) as r:
            urls = _json.load(r)
        names = [u.rsplit("/", 1)[1] for u in urls]
        assert names == ["NotoSansJP", "OpenSans.TTF", "signs.otf"]
        for url, ctype in ((f"/fonts/{sess.sid}/OpenSans.TTF", "font/ttf"),
                           (f"/fonts/{sess.sid}/signs.otf", "font/otf"),
                           (f"/fonts/{sess.sid}/NotoSansJP", "application/octet-stream")):
            with urllib.request.urlopen(base + url, timeout=5) as r:
                assert r.headers.get("Content-Type") == ctype
    finally:
        srv.shutdown()
        sm.shutdown()


def test_refresh_url_throttles_and_never_adopts_src():
    """_refresh_url must (a) rate-limit debrid-resolver lookups per session and
    (b) never set sess.url to the src playback link when the resolver fails —
    that would route every fragment fetch through the resolver (429 storm)."""
    from remuxd import engine as eng_mod
    cfg = Config(session_root="./.remuxd-test-refresh")
    sm = SessionManager(cfg.session_root, 60, 4)
    calls = []
    orig = eng_mod.resolve_final_url
    try:
        eng = Engine(cfg, sm)
        sess = sm.create("seek")
        sess.src, sess.url = "http://debrid/play", "http://cdn/expired"

        # resolver failing: resolve_final_url falls back to returning src
        eng_mod.resolve_final_url = lambda src, ua, h=None: calls.append(src) or src
        out = eng._refresh_url(sess, "http://cdn/expired")
        assert calls == ["http://debrid/play"]         # one lookup
        assert out == "http://cdn/expired"
        assert sess.url == "http://cdn/expired"        # src NOT adopted
        eng._refresh_url(sess, "http://cdn/expired")   # immediately again
        assert len(calls) == 1                          # throttled: no second lookup

        # cooldown elapsed + resolver healthy: URL actually refreshes
        sess.url_refreshed_at = 0.0
        eng_mod.resolve_final_url = \
            lambda src, ua, h=None: calls.append(src) or "http://cdn/fresh"
        assert eng._refresh_url(sess, "http://cdn/expired") == "http://cdn/fresh"
        assert sess.url == "http://cdn/fresh"
        # other threads that failed on the OLD url piggyback, no extra lookup
        assert eng._refresh_url(sess, "http://cdn/expired") == "http://cdn/fresh"
        assert len(calls) == 2
    finally:
        eng_mod.resolve_final_url = orig
        sm.shutdown()


def test_subwindow_caches_dedups_and_warms_ahead():
    """A subtitle window costs tens of MB of upstream fetch, so: a repeat request
    for the same window must not re-extract, concurrent requests for one window
    must share a single extraction, and serving a window must warm the next one."""
    import threading
    from remuxd import engine as eng_mod
    cfg = Config(session_root="./.remuxd-test-subwin")
    sm = SessionManager(cfg.session_root, 60, 4)
    orig = eng_mod.subtitles.extract_window
    calls, gate = [], threading.Event()
    try:
        eng = Engine(cfg, sm)
        sess = sm.create("seek")
        sess.url, sess.header_bytes = "http://cdn/f.mkv", b"HDR"
        sess.tracks = [{"index": 2, "path": "/x", "lang": "eng", "label": "E",
                        "default": True}]
        # 6s windows over 60s of video, 1000 bytes each
        sess.windows = [{"start": i * 6.0, "end": i * 6.0 + 6.0,
                         "boff": 1000 * i, "bend": 1000 * (i + 1)} for i in range(10)]

        def fake(ffmpeg, url, hdr, boff, bend, idx, headers=None):
            calls.append((boff, bend))
            gate.wait(timeout=10)
            return b"Dialogue: 0,0:00:00.00,0:00:01.00,D,,0,0,0,,x"

        eng_mod.subtitles.extract_window = fake

        # two threads want t=0 at once -> one extraction, both get the text
        outs = []
        ts = [threading.Thread(target=lambda: outs.append(eng.subwindow(sess, 0, 0)))
              for _ in range(2)]
        for t in ts:
            t.start()
        time.sleep(0.3)
        gate.set()
        for t in ts:
            t.join(timeout=15)
        assert len(outs) == 2 and all(b"Dialogue:" in o for o in outs)
        first = calls[0]
        assert calls.count(first) == 1                  # deduped, not fetched twice

        # the warm thread pulls the window ahead (t+45) in the background
        deadline = time.monotonic() + 10
        while len(calls) < 2 and time.monotonic() < deadline:
            time.sleep(0.05)
        assert len(calls) >= 2 and calls[1] != first     # a *different* window
        warmed = calls[1]
        time.sleep(0.3)
        n_before = len(calls)

        # repeat of the original window: served from cache, no new extraction
        assert b"Dialogue:" in eng.subwindow(sess, 0, 0)
        # and the warmed-ahead window is already there too (t=45 maps to it)
        assert b"Dialogue:" in eng.subwindow(sess, 0, 45, warm=False)
        assert len(calls) == n_before, f"re-extracted a cached window: {calls}"
        assert warmed  # the ahead-window really was the one t=45 resolves to
    finally:
        eng_mod.subtitles.extract_window = orig
        gate.set()
        sm.shutdown()


def test_extract_window_streams_range_with_absolute_timestamps():
    """extract_window feeds the byte range into ffmpeg as it downloads (rather
    than buffering it whole first) — it must still emit the window's cues with
    -copyts absolute timings, not rebased to zero."""
    import shutil
    import subprocess as sp
    import tempfile
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from remuxd import mkvcues, subtitles

    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        return                                    # no ffmpeg here; nothing to check

    with tempfile.TemporaryDirectory() as d:
        srt = os.path.join(d, "s.srt")
        with open(srt, "w") as f:
            f.write("1\n00:00:02,000 --> 00:00:04,000\nearly cue\n\n"
                    "2\n00:00:20,000 --> 00:00:23,000\nlate cue\n\n")
        mkv = os.path.join(d, "t.mkv")
        r = sp.run(["ffmpeg", "-y", "-loglevel", "error",
                    "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=30",
                    "-i", srt, "-map", "0:v", "-map", "1:s",
                    "-c:v", "libx264", "-preset", "ultrafast", "-g", "30",
                    "-c:s", "ass", mkv], capture_output=True)
        if r.returncode != 0:
            return                                # encoder unavailable in this build
        with open(mkv, "rb") as f:
            body = f.read()

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                rng = self.headers.get("Range")
                if rng and rng.startswith("bytes="):
                    s, _, e = rng[6:].partition("-")
                    start = int(s)
                    end = int(e) if e else len(body) - 1
                    chunk = body[start:end + 1]
                    self.send_response(206)
                    self.send_header("Content-Range",
                                     f"bytes {start}-{end}/{len(body)}")
                else:
                    chunk = body
                    self.send_response(200)
                self.send_header("Content-Length", str(len(chunk)))
                self.end_headers()
                # dribble it out so a buffer-then-run implementation would differ
                for i in range(0, len(chunk), 32 * 1024):
                    self.wfile.write(chunk[i:i + 32 * 1024])

        srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{srv.server_address[1]}/t.mkv"
        try:
            plan = mkvcues.seek_plan(url, None, len(body))
            assert plan, "test file has no Cues index"
            wins = mkvcues.segment_grid(plan, 30.0, 6.0)
            hdr = mkvcues.fetch_range(url, 0, plan["header_size"])
            w = next(x for x in wins if x["start"] <= 20 <= x["end"])
            out = subtitles.extract_window("ffmpeg", url, hdr, w["boff"], w["bend"],
                                           1, None).decode("utf-8", "replace")
            assert "late cue" in out                  # the window's cue came out
            assert "0:00:20.00" in out                # absolute, not rebased to 0
            assert "early cue" not in out             # only this window's clusters
        finally:
            srv.shutdown()


def test_dump_args_sanitizes_attachment_names():
    """Attachment filenames come from the MKV (attacker-controlled): traversal
    must be stripped, empty/duplicate names get synthetic ones, and each stream
    is dumped explicitly by its attachment-relative index."""
    from remuxd.subtitles import dump_args
    args = dump_args([
        {"index": 4, "filename": "OpenSans.ttf"},
        {"index": 5, "filename": "../../../etc/cron.d/evil.ttf"},   # traversal
        {"index": 6, "filename": "fonts\\win\\Arial.ttf"},          # backslashes
        {"index": 7, "filename": ""},                               # no name
        {"index": 8, "filename": "OpenSans.ttf"},                   # duplicate
    ])
    names = args[1::2]
    assert args[0::2] == [f"-dump_attachment:t:{n}" for n in range(5)]
    assert names[0] == "OpenSans.ttf"
    assert names[1] == "evil.ttf"                  # basename only, no traversal
    assert names[2] == "Arial.ttf"
    assert names[3] == "attachment_3"
    assert names[4] == "attachment_4.ttf"          # dedup keeps the extension
    assert all("/" not in n and ".." not in n for n in names)
    assert dump_args(None) == [] and dump_args([]) == []


def test_extract_all_retries_with_refreshed_url():
    """A failed extraction (expired CDN link, 403) must re-resolve the URL and
    retry once — and must not fail silently."""
    import shutil as _shutil
    from remuxd import subtitles

    false_bin = _shutil.which("false")
    assert false_bin, "needs /usr/bin/false"
    fonts_dir = "./.remuxd-test-extract-fonts"
    refreshed = []
    try:
        subtitles.extract_all(
            false_bin, "http://cdn/expired",
            [{"index": 2, "path": os.path.join(fonts_dir, "subs_0.ass")}],
            fonts_dir, ua="UA",
            refresh_url=lambda failed: refreshed.append(failed) or "http://cdn/fresh")
        assert refreshed == ["http://cdn/expired"]     # re-resolve attempted once
    finally:
        _shutil.rmtree(fonts_dir, ignore_errors=True)


def test_prefetch_pauses_when_idle():
    """With no client requests for IDLE_PAUSE, the worker must stop producing
    (an abandoned session shouldn't download the rest of the file), and resume
    when a request touches the session again."""
    import threading
    from remuxd.session import FragmentCache, Session
    from remuxd.engine import PrefetchWorker

    class CountingEngine:
        def __init__(self):
            self._prefetch_sem = threading.Semaphore(2)

        def fragment(self, sess, i):
            return b"", b"m" * 10, None

    sess = Session("idle-test", "seek", "./.idle-test-nodir")
    sess.windows = [{"start": k * 6, "end": k * 6 + 6, "boff": 0, "bend": 10}
                    for k in range(10)]
    sess.cache = FragmentCache(64 * 1024)
    sess.last_access = time.monotonic() - PrefetchWorker.IDLE_PAUSE - 1  # already idle
    w = PrefetchWorker(CountingEngine(), sess, read_ahead=5)
    w.start()
    try:
        time.sleep(0.4)
        assert sess.cache.bytes == 0                  # paused: produced nothing
        sess.touch()                                  # a request arrives...
        w.notify(0)                                   # ...and wakes the worker
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not sess.cache.has(2):
            time.sleep(0.05)
        assert sess.cache.has(0) and sess.cache.has(2)   # resumed
    finally:
        w.stop()


def test_serve_segment_releases_claim_on_error():
    """A fragment() exception must release the in-flight claim — a leaked claim
    makes every later request for that segment block for the full wait."""
    from remuxd.session import FragmentCache
    cfg = Config(session_root="./.remuxd-test-claim")
    sm = SessionManager(cfg.session_root, 60, 4)
    try:
        eng = Engine(cfg, sm)
        sess = sm.create("seek")
        sess.windows = [{"start": 0, "end": 6, "boff": 0, "bend": 10}]
        sess.cache = FragmentCache(1000)
        eng.fragment = lambda s, i: (_ for _ in ()).throw(OSError("fetch failed"))
        try:
            eng.serve_segment(sess, 0)
            assert False, "expected the fragment error to propagate"
        except OSError:
            pass
        should, _ev = sess.cache.claim(0)
        assert should                     # claim was released, not leaked
    finally:
        sm.shutdown()


def test_fragment_closes_response_when_ffmpeg_fails():
    """`resp` is only owned by the feeder thread once it starts; if spawning
    ffmpeg fails before that, fragment() must still close it — otherwise the
    pooled upstream connection leaks on every attempt."""
    from remuxd import engine as engine_mod

    class FakeResp:
        def __init__(self):
            self.closed = False

        def read(self, amt=None):
            return b""

        def close(self):
            self.closed = True

    resp = FakeResp()
    cfg = Config(session_root="./.remuxd-test-leak",
                 ffmpeg="/nonexistent/ffmpeg")     # Popen raises FileNotFoundError
    sm = SessionManager(cfg.session_root, 60, 4)
    orig_open = engine_mod.netio.open_url
    engine_mod.netio.open_url = lambda *a, **k: resp
    try:
        eng = Engine(cfg, sm)
        sess = sm.create("seek")
        sess.windows = [{"start": 0, "end": 6, "boff": 0, "bend": 10}]
        sess.header_bytes = b""
        try:
            eng.fragment(sess, 0)
            assert False, "expected the ffmpeg spawn to fail"
        except FileNotFoundError:
            pass
        assert resp.closed, "upstream response leaked"
    finally:
        engine_mod.netio.open_url = orig_open
        sm.shutdown()


def test_prefetch_survives_fragment_errors():
    """A failed fetch must not kill the worker thread (read-ahead for the whole
    session would silently stop) and must release the failed index's claim."""
    import threading
    from remuxd.session import FragmentCache, Session
    from remuxd.engine import PrefetchWorker

    class FlakyEngine:
        def __init__(self):
            self._prefetch_sem = threading.Semaphore(2)

        def fragment(self, sess, i):
            if i == 1:
                raise OSError("cdn hiccup")
            return b"", b"m" * 10, None

    sess = Session("flaky-test", "seek", "./.flaky-test-nodir")
    sess.windows = [{"start": k * 6, "end": k * 6 + 6, "boff": 0, "bend": 10}
                    for k in range(6)]
    sess.cache = FragmentCache(64 * 1024)
    w = PrefetchWorker(FlakyEngine(), sess, read_ahead=5)
    w.start()
    try:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not sess.cache.has(3):
            time.sleep(0.05)
        assert sess.cache.has(0) and sess.cache.has(2) and sess.cache.has(3)
        assert not sess.cache.has(1)              # the failing one
        should, _ = sess.cache.claim(1)
        assert should                             # its claim was released
    finally:
        w.stop()


# --- synthetic EBML helpers + parser tests ---------------------------------
def _ebml_id(eid):
    return eid.to_bytes((eid.bit_length() + 7) // 8, "big")


def _ebml_size(n):
    assert n < (1 << 28)
    return (0x10000000 | n).to_bytes(4, "big")     # 4-byte length form


def _elem(eid, body):
    return _ebml_id(eid) + _ebml_size(len(body)) + body


def _uint(v, width):
    return v.to_bytes(width, "big")


def _synthetic_mkv_header(with_attachment=False):
    """EBML header + Segment{SeekHead(->Cues), Info(scale), [Attachments], Cues}.
    Returns (buf, seg_off, cues_rel_off, attachment_span_len)."""
    from remuxd import mkvcues as mc
    ebml = _elem(mc.EBML_HEADER, b"")
    info = _elem(mc.INFO, _elem(mc.TIMESTAMP_SCALE, _uint(1_000_000, 3)))
    attach = _elem(mc.ATTACHMENTS, b"F" * 300) if with_attachment else b""
    cue = _elem(mc.CUE_POINT,
                _elem(mc.CUE_TIME, _uint(0, 2)) +
                _elem(mc.CUE_TRACK_POS, _elem(mc.CUE_CLUSTER_POSITION, _uint(5000, 2))))
    cue2 = _elem(mc.CUE_POINT,
                 _elem(mc.CUE_TIME, _uint(6000, 2)) +
                 _elem(mc.CUE_TRACK_POS, _elem(mc.CUE_CLUSTER_POSITION, _uint(9000, 2))))
    cues = _elem(mc.CUES, cue + cue2)
    # SeekHead is the first Segment child; compute the Cues offset (relative to
    # segment data start) from the lengths of what precedes it
    seek_body_probe = _elem(mc.SEEK, _elem(mc.SEEK_ID, _ebml_id(mc.CUES)) +
                            _elem(mc.SEEK_POSITION, _uint(0, 4)))
    seekhead_len = len(_elem(mc.SEEK_HEAD, seek_body_probe))
    cues_rel = seekhead_len + len(info) + len(attach)
    seekhead = _elem(mc.SEEK_HEAD,
                     _elem(mc.SEEK, _elem(mc.SEEK_ID, _ebml_id(mc.CUES)) +
                           _elem(mc.SEEK_POSITION, _uint(cues_rel, 4))))
    assert len(seekhead) == seekhead_len
    seg_body = seekhead + info + attach + cues
    buf = ebml + _ebml_id(mc.SEGMENT) + _ebml_size(len(seg_body)) + seg_body
    seg_off = len(ebml) + len(_ebml_id(mc.SEGMENT)) + 4
    return buf, seg_off, cues_rel, len(attach)


def test_ebml_parser_roundtrip():
    """parse_ebml_header/parse_seek_head/_timestamp_scale/build_cue_points over a
    synthetic Matroska header (the parser previously had zero coverage)."""
    from remuxd import mkvcues as mc
    buf, seg_off, cues_rel, _ = _synthetic_mkv_header()
    assert mc.parse_ebml_header(buf) == seg_off
    seeks = mc.parse_seek_head(buf, seg_off)
    assert seeks[mc.CUES] == cues_rel
    assert mc._timestamp_scale(buf, seg_off) == 1_000_000
    cues_abs = seg_off + cues_rel
    pts = mc.build_cue_points(buf[cues_abs:], 1_000_000, seg_off)
    assert len(pts) == 2
    assert pts[0] == (0.0, seg_off + 5000)
    assert pts[1] == (6.0, seg_off + 9000)      # 6000 ms ticks -> 6 s
    # truncated cues must not raise, just yield what parses
    assert mc.build_cue_points(buf[cues_abs:cues_abs + 8], 1_000_000, seg_off) == []


def test_cues_prefer_video_track_and_stay_monotonic():
    """Regression: a CuePoint may carry CueTrackPositions for several tracks;
    subtitle/audio positions point at clusters far from the video keyframe, and
    naively taking them produced INVERTED byte windows (boff > bend) that fed
    ffmpeg garbage. The parser must pick the video track's positions and drop
    any non-monotonic leftovers."""
    from remuxd import mkvcues as mc

    def track_entry(num, ttype):
        return _elem(mc.TRACK_ENTRY,
                     _elem(mc.TRACK_NUMBER, _uint(num, 1)) +
                     _elem(mc.TRACK_TYPE, _uint(ttype, 1)))

    # header: EBML + Segment{Tracks: video=1, subtitle=3}
    ebml = _elem(mc.EBML_HEADER, b"")
    seg_body = _elem(mc.TRACKS, track_entry(1, 1) + track_entry(3, 0x11))
    buf = ebml + _ebml_id(mc.SEGMENT) + _ebml_size(len(seg_body)) + seg_body
    seg_off = len(ebml) + len(_ebml_id(mc.SEGMENT)) + 4
    assert mc._video_track_number(buf, seg_off) == 1

    def ctp(trk, off):
        return _elem(mc.CUE_TRACK_POS,
                     _elem(mc.CUE_TRACK, _uint(trk, 1)) +
                     _elem(mc.CUE_CLUSTER_POSITION, _uint(off, 4)))

    def cuep(t_ms, *ctps):
        return _elem(mc.CUE_POINT, _elem(mc.CUE_TIME, _uint(t_ms, 2)) + b"".join(ctps))

    # subtitle positions deliberately hostile: huge then tiny (the inverted-window
    # shape from the field); video positions are sane and increasing
    cues = _elem(mc.CUES,
                 cuep(0, ctp(3, 8_000_000), ctp(1, 5000)) +
                 cuep(6000, ctp(3, 100), ctp(1, 9000)) +
                 cuep(12000, ctp(1, 14000), ctp(3, 7_900_000)))
    pts = mc.build_cue_points(cues, 1_000_000, 0, video_track=1)
    assert pts == [(0.0, 5000), (6.0, 9000), (12.0, 14000)]   # video track only

    # unknown video track: falls back to first position per point, and the
    # monotonic filter must still never emit a backward offset
    pts2 = mc.build_cue_points(cues, 1_000_000, 0)
    offs = [o for _, o in pts2]
    assert offs == sorted(offs) and len(set(offs)) == len(offs)

    # segment_grid refuses inverted windows even if bad points get through
    plan = {"cues": [(0.0, 9000), (6.0, 5000)], "file_size": 20000}
    wins = mc.segment_grid(plan, 12.0, target=6.0)
    assert all(w["bend"] is None or w["bend"] > w["boff"] for w in wins)


def test_strip_attachments():
    """Attachments (embedded fonts, tens of MB in the wild) must be spliced out of
    the header prepended to every fragment; other elements survive verbatim."""
    from remuxd import mkvcues as mc
    buf, seg_off, _, attach_len = _synthetic_mkv_header(with_attachment=True)
    assert attach_len > 0
    stripped = mc.strip_attachments(buf, seg_off)
    assert len(stripped) == len(buf) - attach_len
    assert b"F" * 100 not in stripped
    # still a parseable header afterwards, scale intact
    assert mc.parse_ebml_header(stripped) == seg_off
    assert mc._timestamp_scale(stripped, seg_off) == 1_000_000
    # no attachments -> byte-identical
    buf2, seg_off2, _, _ = _synthetic_mkv_header(with_attachment=False)
    assert mc.strip_attachments(buf2, seg_off2) is buf2


def test_pooled_fetch_range():
    """netio range fetches: correct bytes for closed and open-ended ranges, and
    connection reuse across sequential fetches to the same host."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from remuxd import netio

    body = bytes(range(256)) * 4                     # 1024 bytes
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"                # keep-alive so the pool matters

        def log_message(self, *a):
            pass

        def do_GET(self):
            rng = self.headers.get("Range")
            if rng and rng.startswith("bytes="):
                s, _, e = rng[6:].partition("-")
                start = int(s)
                end = int(e) if e else len(body) - 1
                chunk = body[start:end + 1]
                self.send_response(206)
                self.send_header("Content-Range",
                                 f"bytes {start}-{end}/{len(body)}")
            else:
                chunk = body
                self.send_response(200)
            self.send_header("Content-Length", str(len(chunk)))
            self.end_headers()
            self.wfile.write(chunk)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{port}/f"
    try:
        assert netio.fetch_range(url, 10, 20, "UA") == body[10:20]
        assert netio.fetch_range(url, 1000, None, "UA") == body[1000:]   # to EOF
        with netio._pool_lock:
            pooled = sum(len(v) for v in netio._pool.values())
        assert pooled >= 1                           # connection went back to the pool
    finally:
        srv.shutdown()


def test_head_is_readonly():
    """HEAD is routed through do_GET so the demo can poll Content-Length on
    growing .ass files — it must not reach the endpoints with side effects."""
    import threading
    from http.server import ThreadingHTTPServer
    from remuxd.server import Handler

    cfg = Config(port=0, session_root="./.remuxd-test-head")
    sm = SessionManager(cfg.session_root, ttl_seconds=60, max_sessions=4)
    Handler.engine = Engine(cfg, sm)
    Handler.config = cfg
    Handler.resolver = None
    srv = ThreadingHTTPServer((cfg.host, 0), Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"

    def head(path):
        r = urllib.request.Request(base + path, method="HEAD")
        try:
            with urllib.request.urlopen(r, timeout=5) as resp:
                return resp.status
        except urllib.error.HTTPError as e:
            return e.code

    try:
        # a HEAD /start would resolve, probe and spin up a whole session
        assert head("/start?src=http://example.invalid/a.mkv") == 405
        assert head("/resolve?anilist=1") == 405
        assert head("/stop/whatever") == 404       # POST-only endpoint
        assert not sm._sessions                    # nothing was created
        assert head("/hls/nosuch/index.m3u8") == 404   # read paths still route
    finally:
        srv.shutdown()
        sm.shutdown()


def test_fonts_with_spaces_roundtrip():
    """System-font filenames contain spaces ("Trebuchet MS.ttf"): /fontlist must
    percent-encode them and /fonts must decode — unencoded they 404 and the
    renderer silently falls back (regression). Traversal stays blocked encoded."""
    import threading
    from http.server import ThreadingHTTPServer
    from remuxd.server import Handler

    cfg = Config(port=0, session_root="./.remuxd-test-fonts", demo=False)
    sm = SessionManager(cfg.session_root, ttl_seconds=60, max_sessions=4)
    Handler.engine = Engine(cfg, sm)
    Handler.config = cfg
    Handler.resolver = None
    srv = ThreadingHTTPServer((cfg.host, 0), Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"

    def get(path):
        try:
            with urllib.request.urlopen(base + path, timeout=5) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    try:
        sess = sm.create("seek")
        sess.tracks = [{"index": 3, "path": "x", "lang": "en",
                        "label": "E", "default": True}]
        sess.subs_started = True                      # skip real extraction
        os.makedirs(sess.fonts_dir(), exist_ok=True)
        font = b"\x00\x01\x00\x00" + b"F" * 64
        with open(os.path.join(sess.fonts_dir(), "Trebuchet MS.ttf"), "wb") as f:
            f.write(font)
        import json as _json
        st, body = get(f"/fontlist/{sess.sid}")
        assert st == 200
        urls = _json.loads(body)
        assert urls == [f"/fonts/{sess.sid}/Trebuchet%20MS.ttf"]
        st, body = get(urls[0])                       # encoded name round-trips
        assert st == 200 and body == font
        st, _ = get(f"/fonts/{sess.sid}/..%2f..%2fsecret")   # encoded traversal
        assert st == 404
    finally:
        srv.shutdown()
        sm.shutdown()


def test_ass_style_font_matching():
    """Sources with NO embedded fonts name system fonts in their ASS styles;
    those must be found and served (regression: CR WEB-DLs looked right in VLC
    but fell back to the default font in the browser renderer)."""
    import tempfile
    from remuxd import subtitles

    ass = ("[Script Info]\nTitle: x\n\n[V4+ Styles]\n"
           "Format: Name,Fontname,Fontsize\n"
           "Style: Default,Trebuchet MS,24\n"
           "Style: Signs,@Custom Font,24\n"
           "Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,{\\fnVerdana\\b1}hi\n")
    assert subtitles.ass_fontnames(ass) == {"Trebuchet MS", "Custom Font", "Verdana"}

    fdir = tempfile.mkdtemp()
    for fn in ("Custom Font.ttf", "Custom Font Bold.ttf", "CustomFontier.ttf",
               "Custom Font.txt", "Other.ttf"):
        with open(os.path.join(fdir, fn), "wb") as f:
            f.write(b"\x00\x01\x00\x00x")
    orig = subtitles._SYSTEM_FONT_DIRS
    subtitles._SYSTEM_FONT_DIRS = [fdir, "/nonexistent"]
    try:
        hits = sorted(os.path.basename(p)
                      for p in subtitles._find_system_fonts("custom font"))
        # exact + weight variants match; unrelated stems and non-fonts don't
        assert hits == ["Custom Font Bold.ttf", "Custom Font.ttf"]
        assert subtitles._find_system_fonts("nope") == []
    finally:
        subtitles._SYSTEM_FONT_DIRS = orig


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print("ok:", fn.__name__)
    print(f"\n{len(fns)} tests passed")
