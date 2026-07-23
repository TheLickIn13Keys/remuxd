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
    sm = SessionManager(tmp_root, ttl_seconds=1, max_sessions=2)
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


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print("ok:", fn.__name__)
    print(f"\n{len(fns)} tests passed")
