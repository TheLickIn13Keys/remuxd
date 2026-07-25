"""The remux engine: decide how to serve a source and drive the session.

``Engine.start`` probes a URL and picks one of three strategies:
  passthrough  browser-native (MP4/H.264-8bit/AAC) -> proxy the bytes.
  seek-copy    decodable codec + MKV Cues index -> synthesized VOD playlist,
               each keyframe-aligned fMP4 fragment produced on demand.
  singlepass   forced transcode / undecodable codec / no index -> live HLS.
It returns a JSON-able descriptor whose URLs are all scoped to the new session.
"""
import logging
import os
import subprocess
import threading
import time
import uuid
from typing import Dict, Optional

from . import mkvcues
from . import media
from . import netio
from . import subtitles
from .config import Config
from .netio import content_length, resolve_final, resolve_final_url
from .session import FragmentCache, Session, SessionManager
from .timing import Timeline

log = logging.getLogger("remuxd.engine")


class StreamError(RuntimeError):
    """A source can't be turned into a playable stream."""


class _PrepEntry:
    """Memoized per-source prep. final/fsize/probed are filled at start; windows +
    header_bytes are filled lazily by the seek path. Byte offsets and header bytes
    stay valid even if the CDN link later expires (same file), so only the URL may
    need a re-resolve, which fragment() already handles."""
    __slots__ = ("final", "fsize", "probed", "windows", "header_bytes", "ts")

    def __init__(self, final, fsize, probed, ts):
        self.final = final
        self.fsize = fsize
        self.probed = probed
        self.windows = None
        self.header_bytes = None
        self.ts = ts


class Engine:
    _PREP_MAX = 32

    def __init__(self, config: Config, sessions: SessionManager):
        self.cfg = config
        self.sessions = sessions
        # Caps concurrent prefetch ffmpeg jobs across sessions; on-demand never waits.
        self._prefetch_sem = threading.Semaphore(max(1, config.prefetch_concurrency))
        # Per-source prep memo (see _PrepEntry), keyed by (src, headers).
        self._prep: Dict[tuple, _PrepEntry] = {}
        self._prep_lock = threading.Lock()

    def _prep_get(self, key):
        if self.cfg.prep_cache_ttl <= 0:
            return None
        with self._prep_lock:
            e = self._prep.get(key)
            if e is None:
                return None
            if time.monotonic() - e.ts <= self.cfg.prep_cache_ttl:
                return e
            self._prep.pop(key, None)
            return None

    def _prep_put(self, key, entry):
        if self.cfg.prep_cache_ttl <= 0:
            return
        with self._prep_lock:
            if len(self._prep) >= self._PREP_MAX and key not in self._prep:
                oldest = min(self._prep, key=lambda k: self._prep[k].ts)
                self._prep.pop(oldest, None)
            self._prep[key] = entry

    def start(self, src: str, mode: str = "auto",
              headers: Optional[Dict[str, str]] = None,
              want_audio: Optional[int] = None) -> dict:
        cfg = self.cfg
        tl = Timeline(log, "start-" + uuid.uuid4().hex[:6])
        key = (src, tuple(sorted((headers or {}).items())))
        entry = self._prep_get(key)
        if entry is not None:
            # repeat /start on this src (audio switch, replay): skip resolve+probe
            final, fsize, probed = entry.final, entry.fsize, entry.probed
            tl.lap("prep_cache")
        else:
            # Resolve the debrid/redirect URL to the real CDN link ONCE: these re-run
            # a lookup per hit and 429 if touched repeatedly. Capture the file size from
            # the same response (Content-Range) instead of probing it again later.
            final, fsize = resolve_final(src, cfg.user_agent, headers)
            tl.lap("resolve_url")
            try:
                probed = media.probe(cfg.ffprobe, final, headers, ua=cfg.user_agent)
            except Exception as e:
                raise StreamError(f"probe failed: {e}") from e
            tl.lap("probe")
            entry = _PrepEntry(final, fsize, probed, time.monotonic())
            self._prep_put(key, entry)
        v, pf, a, cont = probed.vcodec, probed.pix_fmt, probed.acodec, probed.container
        aidx, audios = probed.aidx, probed.audios
        if not v and not audios:
            raise StreamError("source has no video or audio streams "
                              "(dead link or unsupported file)")
        chosen = media.choose_audio(audios, want_audio)
        if chosen:
            aidx, a = chosen["index"], chosen["codec"]
        audio_meta = media.audio_meta(audios, aidx)
        amap = f"0:{aidx}" if aidx is not None else "0:a:0?"

        if mode in ("auto", "remux") and media.is_passthrough(v, pf, a, cont):
            sess = self.sessions.create("passthrough")
            sess.url, sess.headers, sess.src = final, headers, src
            tl.done(f"passthrough sid={sess.sid}")
            return self._info(sess, v, pf, a, cont, "passthrough",
                              "direct-play (no ffmpeg)", "direct-play (no ffmpeg)",
                              playlist=f"/proxy/{sess.sid}", tracks=[], audio_meta=audio_meta)

        if mode in ("auto", "remux") and media.vcodec_copyable(v, pf):
            info = self._start_seek(final, src, headers, probed, amap, audio_meta,
                                    want_audio, fsize=fsize, prep=entry, tl=tl)
            if info is not None:
                return info
            # No usable Cues -> single-pass copy->HLS (ffmpeg picks the cuts).
            return self._start_singlepass(final, src, headers, "remux", probed,
                                          want_audio, audio_meta, tl=tl)

        # Transcode (forced, or undecodable codec). Prefer per-segment seek-transcode
        # for full seeking when we have a Cues index; else fall back to live TS.
        info = self._start_seek(final, src, headers, probed, amap, audio_meta,
                                want_audio, transcode=True, fsize=fsize, prep=entry, tl=tl)
        if info is not None:
            return info
        return self._start_singlepass(final, src, headers, mode, probed, want_audio,
                                      audio_meta, tl=tl)

    def _start_seek(self, final, src, headers, probed, amap, audio_meta, want_audio,
                    transcode=False, fsize=None, prep=None, tl=None):
        """Per-segment on-demand path (copy or transcode). Needs an MKV Cues index;
        returns None (caller falls back to singlepass) when it can't be used."""
        cfg = self.cfg
        v, pf, a, cont = probed.vcodec, probed.pix_fmt, probed.acodec, probed.container
        if probed.duration <= 0:
            return None
        if not ("matroska" in (cont or "") or "webm" in (cont or "")):
            return None

        if prep is not None and prep.windows is not None:
            windows, header_bytes = prep.windows, prep.header_bytes   # cached prep
            if tl:
                tl.lap("prep_cache_seek")
        else:
            if fsize is None:                   # resolve_final couldn't read it
                fsize = content_length(final, cfg.user_agent, headers)
                if tl:
                    tl.lap("content_len")
            plan = mkvcues.seek_plan(final, headers, fsize)
            if tl:
                tl.lap("cues_index")
            if not plan:
                return None
            windows = mkvcues.segment_grid(plan, probed.duration, cfg.segment_seconds)
            if not windows:
                return None
            raw_header = mkvcues.fetch_range(final, 0, plan["header_size"], headers)
            # Fonts/attachments can be tens of MB and ride along on EVERY fragment
            # pipe otherwise; the A/V remux never reads them.
            header_bytes = mkvcues.strip_attachments(raw_header, plan["segment_offset"])
            if len(header_bytes) != len(raw_header):
                log.info("stripped %dKB of attachments from MKV header (%dKB -> %dKB)",
                         (len(raw_header) - len(header_bytes)) // 1024,
                         len(raw_header) // 1024, len(header_bytes) // 1024)
            if tl:
                tl.lap("header_fetch")
            if prep is not None:
                prep.windows, prep.header_bytes = windows, header_bytes

        aac = a in ("aac", "mp3")
        sess = self.sessions.create("seek-transcode" if transcode else "seek")
        sess.url, sess.src, sess.headers = final, src, headers
        sess.windows, sess.header_bytes = windows, header_bytes
        sess.aac, sess.amap, sess.file_size = aac, amap, fsize
        sess.transcode = transcode
        sess.tracks = subtitles.sub_tracks(probed.subs, sess.dir)
        sess.attachments = probed.attachments
        sess.start_time = probed.start_time
        sess.cache = FragmentCache(cfg.fragment_cache_bytes)
        if cfg.prefetch_segments > 0:
            sess.prefetch = PrefetchWorker(self, sess, cfg.prefetch_segments)
            sess.prefetch.start()
        if tl:
            tl.done(f"{'seek-transcode' if transcode else 'seek-copy'} "
                    f"sid={sess.sid} segs={len(windows)}")
        video_action = (f"transcode->h264 · {len(windows)} segments" if transcode
                        else f"copy ({v}) · {len(windows)} keyframe segments")
        return self._info(
            sess, v, pf, a, cont, "seek-transcode" if transcode else "seek-copy",
            video_action, f"copy ({a})" if aac else f"transcode->aac ({a})",
            playlist=f"/hls/{sess.sid}/index.m3u8",
            tracks=sess.tracks, audio_meta=audio_meta, seekable=True)

    def _start_singlepass(self, final, src, headers, mode, probed, want_audio,
                          audio_meta, tl=None):
        cfg = self.cfg
        sess = self.sessions.create("singlepass")
        sess.url, sess.src, sess.headers = final, src, headers
        cmd, info = media.build_hls_cmd(cfg.ffmpeg, final, sess.dir, mode, headers,
                                        probed=probed, want_audio=want_audio,
                                        ffprobe=cfg.ffprobe, venc=cfg.video_encode_args,
                                        ua=cfg.user_agent)
        sess.tracks = subtitles.sub_tracks(info.get("subs_streams") or [], sess.dir)
        sess.attachments = probed.attachments if probed else []
        sess.start_time = probed.start_time if probed else 0.0
        sess.proc = subprocess.Popen(cmd, stderr=subprocess.PIPE)
        proc = sess.proc

        playlist = os.path.join(sess.dir, "index.m3u8")
        for _ in range(120):   # up to ~30s (remote probe + first segment)
            if os.path.exists(playlist) and os.path.getsize(playlist) > 0:
                break
            if proc.poll() is not None:
                err = (proc.stderr.read().decode(errors="replace")[-800:]
                       if proc.stderr else "")
                self.sessions.remove(sess.sid)
                raise StreamError(f"ffmpeg exited early:\n{err}")
            time.sleep(0.25)
        else:
            self.sessions.remove(sess.sid)
            raise StreamError("timed out waiting for first HLS segment")
        if tl:
            tl.lap("first_segment")
            tl.done(f"singlepass sid={sess.sid}")
        return self._info(
            sess, info["video"], info["pix_fmt"], info["audio"], info["container"],
            info["mode"], info["video_action"], info["audio_action"],
            playlist=f"/hls/{sess.sid}/index.m3u8",
            tracks=sess.tracks, audio_meta=audio_meta)

    # Each per-segment ffmpeg gets this long; segment waiters allow a bit more
    # (a waiter that gives up before the producer's deadline duplicates its work).
    FRAGMENT_TIMEOUT = 180

    # Minimum seconds between debrid-resolver lookups per session. A dead link
    # plus the prefetch worker's retries would otherwise hammer it into 429s.
    _REFRESH_COOLDOWN = 30.0

    def _refresh_url(self, sess: Session, failed_url: str) -> str:
        """Re-resolve the CDN link, serialized per session so concurrent fetch
        failures piggyback on the first lookup. Callers get the current URL
        unchanged when throttled by _REFRESH_COOLDOWN."""
        with sess.url_lock:
            if sess.url != failed_url:
                return sess.url          # someone already refreshed it
            now = time.monotonic()
            if now - sess.url_refreshed_at < self._REFRESH_COOLDOWN:
                return sess.url          # throttled: don't touch the resolver
            sess.url_refreshed_at = now
            new = resolve_final_url(sess.src, self.cfg.user_agent, sess.headers)
            # resolve_final_url falls back to returning src on failure. Never
            # adopt that: src is the debrid playback link that re-runs a lookup
            # on every hit, so each later fetch would re-hit the resolver.
            if new and new != sess.src:
                sess.url = new
            return sess.url

    # Backoff bounds for a rate-limited host that sends no Retry-After.
    _THROTTLE_BASE = 2.0
    _THROTTLE_MAX = 60.0

    # Prefetch pacing (see Session.prefetch_gap). AIMD: a refusal multiplies the
    # gap, each success decays it, so read-ahead settles just under the host's
    # limit instead of repeatedly sprinting into it.
    _PACE_MIN = 0.5
    _PACE_MAX = 15.0
    _PACE_DECAY = 0.85

    def _note_throttle(self, sess: Session, exc: BaseException) -> None:
        """Record a 429/503 so every fetch path for this session backs off, not
        just the one that got refused. Honors Retry-After; without one the wait
        doubles for as long as the host keeps refusing.

        INFO while the backoff sits at its floor (read-ahead finding the limit and
        settling under it, which is the design working); WARNING once it starts
        doubling, which means we're losing ground."""
        if getattr(exc, "status", None) not in (429, 503):
            return
        wait = getattr(exc, "retry_after", None)
        with sess.throttle_lock:
            if wait is None:
                prev = sess.throttle_backoff or self._THROTTLE_BASE / 2
                wait = prev * 2
            wait = max(self._THROTTLE_BASE, min(self._THROTTLE_MAX, wait))
            sess.throttle_backoff = wait
            sess.throttled_until = max(sess.throttled_until, time.monotonic() + wait)
            # Pausing alone just delays the next sprint into the limit; slow the
            # steady-state read-ahead rate too.
            sess.prefetch_gap = min(self._PACE_MAX,
                                    max(self._PACE_MIN, sess.prefetch_gap * 2))
            # same message either way, so one grep finds every refusal
            emit = log.info if wait <= self._THROTTLE_BASE else log.warning
            emit("[throttle %s] upstream refused (%s); backing off %.0fs, "
                 "pacing read-ahead at %.1fs/fragment",
                 sess.sid, getattr(exc, "status", "?"), wait, sess.prefetch_gap)

    def _clear_throttle(self, sess: Session) -> None:
        """A successful fetch means the host is serving us again. The pace decays
        rather than dropping to zero: one success doesn't prove the limiter's
        window has reopened, and going straight back to full speed is what makes
        read-ahead bounce off the limit every few seconds."""
        if sess.throttle_backoff or sess.throttled_until or sess.prefetch_gap:
            with sess.throttle_lock:
                sess.throttle_backoff = 0.0
                sess.throttled_until = 0.0
                gap = sess.prefetch_gap * self._PACE_DECAY
                sess.prefetch_gap = 0.0 if gap < self._PACE_MIN / 2 else gap

    # A client blocked on a segment waits this long across rate-limit retries
    # before we give up. Long enough to ride out a short limiter window; short
    # enough that a genuinely dead link still fails while the player is waiting.
    _WAIT_THROTTLED_DEADLINE = 20.0
    _WAIT_THROTTLED_SLEEP_MAX = 5.0

    def _open_ranged(self, sess: Session, boff: int, bend, wait_throttled: bool):
        """Open bytes [boff,bend) of the session's source under the session-wide
        rate-limit policy, and return the open response.

        Every large ranged read for a session goes through here: the host counts
        them all against one limit, so a path that fetched around this one would
        walk into 429s the rest of the session already knows are coming."""
        def open_win(url):
            return netio.open_url(url, self.cfg.user_agent, sess.headers,
                                  rng=(boff, bend), timeout=60)

        deadline = time.monotonic() + (self._WAIT_THROTTLED_DEADLINE
                                       if wait_throttled else 0.0)
        while True:
            if wait_throttled:
                # ride out a backoff another fetch already earned, instead of
                # walking into a refusal we know is coming
                pause = min(sess.throttle_wait(), self._WAIT_THROTTLED_SLEEP_MAX,
                            max(0.0, deadline - time.monotonic()))
                if pause > 0:
                    time.sleep(pause)
            old_url = sess.url
            try:
                resp = open_win(old_url)
                break
            except Exception as exc:
                if getattr(exc, "status", None) in (429, 503):
                    # Not link expiry: a re-resolve costs a resolver round-trip
                    # and the fresh link points at the same refusing host.
                    self._note_throttle(sess, exc)
                    if wait_throttled and time.monotonic() < deadline:
                        continue
                    raise
                new_url = self._refresh_url(sess, old_url)
                if new_url == old_url:
                    raise            # cooldown, or the resolver had nothing new
                try:
                    resp = open_win(new_url)
                except Exception as retry_exc:   # a second failure propagates
                    self._note_throttle(sess, retry_exc)
                    raise
                break
        self._clear_throttle(sess)
        return resp

    def fragment(self, sess: Session, i: int, wait_throttled: bool = False):
        """Produce fragment i: stream its cluster bytes (+ MKV header) into ffmpeg
        as they download, so fetch and remux overlap (cold latency ~ max(fetch,
        remux) instead of their sum) and the window is never buffered whole in
        memory. Returns (init, media_bytes, err); re-resolves the link once if
        opening the byte range fails.

        ``wait_throttled`` is for fetches a client is blocked on: a 429 is waited
        out and retried rather than raised. Read-ahead leaves it False, yielding
        its slot to the segments someone is actually waiting for."""
        w = sess.windows[i]
        t0 = time.perf_counter()
        resp = self._open_ranged(sess, w["boff"], w["bend"], wait_throttled)
        t_open = (time.perf_counter() - t0) * 1000

        t1 = time.perf_counter()
        killed = threading.Event()
        p = timer = None
        fed_owns_resp = False
        fed = [0]

        def _kill():
            killed.set()
            p.kill()

        def feed():
            try:
                p.stdin.write(sess.header_bytes)
                while True:
                    chunk = resp.read(256 * 1024)
                    if not chunk:
                        break
                    p.stdin.write(chunk)
                    fed[0] += len(chunk)
            except (BrokenPipeError, OSError):
                pass               # ffmpeg exited/killed, or upstream dropped
            finally:
                try:
                    p.stdin.close()
                except OSError:
                    pass
                resp.close()

        err_buf = []
        # Until feeder.start() succeeds nothing else will ever close `resp`, so
        # everything up to the handoff has to clean up after itself; otherwise a
        # failure here (no ffmpeg, thread limit) leaks a pooled connection and
        # orphans the child.
        try:
            p = subprocess.Popen(
                media.segment_transcode_cmd(self.cfg.ffmpeg, self.cfg.video_encode_args,
                                            sess.aac, sess.amap)
                if sess.transcode
                else media.segment_cmd(self.cfg.ffmpeg, sess.aac, sess.amap),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            timer = threading.Timer(self.FRAGMENT_TIMEOUT, _kill)
            timer.daemon = True
            timer.start()
            feeder = threading.Thread(target=feed, name=f"feed-{sess.sid}#{i}",
                                      daemon=True)
            # stderr drained on its own thread so ffmpeg can't deadlock on a full pipe
            drainer = threading.Thread(target=lambda: err_buf.append(p.stderr.read()),
                                       daemon=True)
            feeder.start()
            fed_owns_resp = True     # from here on feed()'s finally closes it
            drainer.start()
        except BaseException:
            if timer is not None:
                timer.cancel()
            if p is not None:
                p.kill()
                p.wait()
            if not fed_owns_resp:
                resp.close()
            raise
        out = p.stdout.read()
        p.wait()
        timer.cancel()
        feeder.join(timeout=10)
        drainer.join(timeout=10)
        err = err_buf[0] if err_buf else b""
        t_ff = (time.perf_counter() - t1) * 1000
        if killed.is_set():
            log.warning("[seg %d] ffmpeg timed out", i)
            return None, None, b"ffmpeg timeout"
        if not out:
            log.warning("[seg %d] empty window=%.2f-%.2f bytes[%s:%s] stderr=%s",
                        i, w["start"], w["end"], w["boff"], w["bend"],
                        err.decode(errors="replace").strip()[-500:])
            return None, None, err
        init, media_bytes = media.split_init(out)
        log.debug("[frag %s#%d] open=%.0fms fed=%dKB %s=%.0fms out=%dKB",
                  sess.sid, i, t_open, fed[0] // 1024,
                  "encode" if sess.transcode else "remux", t_ff, len(out) // 1024)
        return init, media_bytes, None

    def serve_segment(self, sess: Session, i: int, move_playhead: bool = True
                      ) -> Optional[bytes]:
        """Fragment i for a seek session: from the read-ahead cache if warm, from
        an in-flight producer if one is already fetching it (wait, don't re-fetch),
        else produced on demand. Moves the playhead so the worker reads ahead.

        ``move_playhead=False`` produces the fragment without repointing
        read-ahead, for callers that aren't the client's actual position."""
        if i < 0 or i >= len(sess.windows):
            return None
        if sess.prefetch and move_playhead:
            sess.prefetch.notify(i)
        if not sess.cache:
            _init, media_bytes, _ = self.fragment(sess, i, wait_throttled=True)
            return media_bytes

        cached = sess.cache.get(i)
        if cached is not None:
            log.debug("[seg %s#%d] cache-hit (%dKB)", sess.sid, i, len(cached) // 1024)
            return cached
        should, ev = sess.cache.claim(i)
        if not should:
            if ev is None:      # raced with a producer: cached between get and claim
                return sess.cache.get(i)
            # someone else (usually the prefetch worker) is already fetching it;
            # wait for their result instead of re-downloading the same bytes. The
            # wait must outlast the producer's ffmpeg deadline or we'd duplicate
            # work the producer is still doing.
            ev.wait(timeout=self.FRAGMENT_TIMEOUT + 60)
            cached = sess.cache.get(i)
            if cached is not None:
                log.debug("[seg %s#%d] in-flight-hit (%dKB)", sess.sid, i, len(cached) // 1024)
                return cached
            should, _ev = sess.cache.claim(i)   # producer failed/evicted -> take over
            if not should:
                # cached in the meantime, or a producer is somehow still running
                # past its deadline. Either way, don't pile on another fetch.
                return sess.cache.get(i)

        try:
            init, media_bytes, _err = self.fragment(sess, i, wait_throttled=True)
        except Exception:
            sess.cache.release(i)   # never leak the claim: waiters would hang on it
            raise
        if media_bytes is None:
            sess.cache.release(i)
            return None
        sess.cache.put(i, media_bytes, sess.playhead)
        if init:
            with sess.lock:
                if sess.init is None:
                    sess.init = init
        return media_bytes

    def serve_init(self, sess: Session):
        """The shared init.mp4 (ftyp+moov). Produced by warming segment 0 through the
        cache, so it shares the fetch with prefetch instead of duplicating it.
        serve_segment sets sess.init as a side effect.

        Deliberately does NOT move the playhead: init.mp4 says nothing about where
        the client is watching, and a player re-fetching it after a seek would
        otherwise drag read-ahead back to segment 0 and abandon a warm window."""
        if sess.init is None:
            self.serve_segment(sess, 0, move_playhead=False)
        return sess.init

    # Past subtitle windows kept per session. Each is a few KB of text (the MBs
    # fetched to produce it aren't retained), so this is cheap.
    _SUBWIN_CACHE_MAX = 64

    # Seconds ahead of a requested window to warm in the background: playback
    # runs off the end of a window faster than the next one takes to fetch.
    _SUBWIN_AHEAD = 45.0

    def subwindow(self, sess: Session, n: int, t: float, warm: bool = True) -> bytes:
        """On-demand ASS around time ``t``: extract track n from the clusters
        spanning ~[t-10, t+45] with -copyts (preserve absolute timestamps).
        Re-resolves the link once if the fetch comes back empty.

        Results are cached per byte window and concurrent callers of the same
        window share one extraction. ``warm`` also pulls the following window in
        the background; it is False on those calls so warming never chains."""
        wins = sess.windows or []
        if n < 0 or n >= len(sess.tracks):
            return b""
        lo, hi = max(0.0, t - 10), t + 45
        boff = bend = None
        for w in wins:
            if w["end"] >= lo and w["start"] <= hi:
                boff = w["boff"] if boff is None else min(boff, w["boff"])
                be = w["bend"] if w["bend"] is not None else sess.file_size
                if be is not None:
                    bend = be if bend is None else max(bend, be)
        if boff is None:
            return b""
        key = (n, boff, bend)

        while True:
            with sess.subwin_lock:
                if key in sess.subwin:
                    return sess.subwin[key]
                waiting = sess.subwin_inflight.get(key)
                if waiting is None:
                    done = threading.Event()
                    sess.subwin_inflight[key] = done
                    break
            # someone else is extracting this exact window: ride along, and if
            # their attempt failed report that rather than re-paying the fetch
            waiting.wait(timeout=180)
            with sess.subwin_lock:
                return sess.subwin.get(key, b"")

        out = b""
        idx = sess.tracks[n]["index"]

        # This pulls the same tens of MB a video fragment does, so it goes through
        # the session's ranged opener (shared backoff, link refresh, 429 retry).
        # Only ``warm`` calls have a client blocked on them; the background top-up
        # must not sit waiting a refusal out.
        def open_win():
            return self._open_ranged(sess, boff, bend, wait_throttled=warm)

        try:
            out = subtitles.extract_window(
                self.cfg.ffmpeg, sess.url, sess.header_bytes, boff, bend, idx,
                sess.headers, start_time=sess.start_time, open_range=open_win)
        except Exception:
            out = b""
        finally:
            with sess.subwin_lock:
                if out:
                    if len(sess.subwin) >= self._SUBWIN_CACHE_MAX:
                        sess.subwin.pop(next(iter(sess.subwin)), None)   # oldest
                    sess.subwin[key] = out
                sess.subwin_inflight.pop(key, None)
                done.set()
        if out and warm:
            threading.Thread(
                target=self._warm_subwindow, args=(sess, n, t + self._SUBWIN_AHEAD),
                name=f"subwarm-{sess.sid}", daemon=True).start()
        return out

    def _warm_subwindow(self, sess: Session, n: int, t: float) -> None:
        # Through the prefetch budget: this pulls the same tens of MB a video
        # fragment does, and it must never outbid the fragments playback needs.
        if not self._prefetch_sem.acquire(timeout=30):
            return
        try:
            self.subwindow(sess, n, t, warm=False)
        except Exception:
            log.debug("subwindow warm failed", exc_info=True)
        finally:
            self._prefetch_sem.release()

    def ensure_sub_extraction(self, sess: Session) -> None:
        """Kick off the full-file subtitle+font extraction on first demand. Lazy
        because it downloads the ENTIRE source: sessions whose client never asks
        for subs (or only uses /subwindow) skip a whole-file read."""
        if not sess.tracks or not sess.url or sess.subs_started:
            return
        with sess.lock:
            if sess.subs_started:
                return
            sess.subs_started = True
        threading.Thread(
            target=subtitles.extract_all,
            args=(self.cfg.ffmpeg, sess.url, sess.tracks, sess.fonts_dir(),
                  sess.headers),
            kwargs={"ua": self.cfg.user_agent,
                    "attachments": sess.attachments,
                    "refresh_url": lambda failed: self._refresh_url(sess, failed)},
            name=f"subs-{sess.sid}", daemon=True).start()

    def _info(self, sess, video, pix_fmt, audio, container, mode, video_action,
              audio_action, playlist, tracks, audio_meta, seekable=False) -> dict:
        has_subs = bool(tracks)
        return {
            "sid": sess.sid,
            "video": video, "pix_fmt": pix_fmt, "audio": audio, "container": container,
            "mode": mode, "video_action": video_action, "audio_action": audio_action,
            "playlist": playlist,
            "subs": f"/subs/{sess.sid}" if has_subs else None,
            "tracks": subtitles.track_meta(tracks, sess.sid),
            "fontlist": f"/fontlist/{sess.sid}" if has_subs else None,
            "subwindow": f"/subwindow/{sess.sid}" if (has_subs and seekable) else None,
            "audios": audio_meta,
        }


class PrefetchWorker(threading.Thread):
    """Keeps a seek session's fragments warm ahead of the playhead.

    Each pass produces the not-yet-cached fragments in [playhead, playhead+read_ahead],
    then waits for the playhead to move or a short timeout. A seek moves the playhead
    either direction and the worker abandons its window mid-pass to refocus, so it
    warms the seek target promptly instead of finishing the stale window. Large
    read_ahead ~= download the whole file ahead, bounded by the cache byte budget.

    If the client stops requesting anything for IDLE_PAUSE seconds (switched to
    another stream, closed the tab without /stop), the worker pauses instead of
    downloading the rest of the file for nobody; the next request resumes it.
    """

    IDLE_PAUSE = 60.0

    def __init__(self, engine: "Engine", sess: Session, read_ahead: int):
        super().__init__(name=f"prefetch-{sess.sid}", daemon=True)
        self.engine = engine
        self.sess = sess
        self.read_ahead = read_ahead
        self._wake = threading.Event()
        self._stop = threading.Event()

    def notify(self, index: int) -> None:
        # Playhead follows the client either direction; a backward seek must
        # re-point read-ahead, not be ignored.
        self.sess.playhead = index
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def run(self) -> None:
        sess = self.sess
        n = len(sess.windows or [])
        idle_wait = 2.0
        while not self._stop.is_set():
            if time.monotonic() - sess.last_access > self.IDLE_PAUSE:
                # nobody is watching; idle until a request touches the session
                # (notify() also wakes us immediately on the next segment hit)
                self._wake.wait(timeout=5.0)
                self._wake.clear()
                continue
            produced = refocus = errors = False
            throttled = 0.0
            ph = sess.playhead
            hi = min(n, ph + 1 + self.read_ahead)
            for i in range(ph, hi):
                if self._stop.is_set():
                    break
                # the host is rate-limiting this session, and read-ahead is the
                # load it can most afford to lose (on-demand fetches still go
                # through). Stop the pass on the first refusal, not after 32.
                throttled = sess.throttle_wait()
                if throttled > 0:
                    break
                if sess.playhead != ph:
                    refocus = True    # playhead moved -> rebuild the window now
                    break
                if sess.cache.bytes >= sess.cache.budget:
                    break             # full; wait for playback to evict behind us
                should, _ev = sess.cache.claim(i)
                if not should:
                    continue          # cached or another producer has it
                if not self.engine._prefetch_sem.acquire(timeout=5):
                    sess.cache.release(i)
                    break
                _init = media_bytes = None
                try:
                    if not self._stop.is_set():
                        _init, media_bytes, _err = self.engine.fragment(sess, i)
                except Exception as exc:
                    # a failed fetch must not kill the worker (read-ahead for the
                    # whole session would silently stop). _note_throttle already
                    # reported any rate-limit refusal with the backoff it chose,
                    # so only an unexpected error gets a traceback here.
                    if getattr(exc, "status", None) in (429, 503):
                        log.debug("[prefetch %s#%d] %s", sess.sid, i, exc)
                    elif getattr(exc, "status", None):
                        log.warning("[prefetch %s#%d] %s", sess.sid, i, exc)
                    else:
                        log.warning("[prefetch %s#%d] failed", sess.sid, i,
                                    exc_info=True)
                    errors = True
                finally:
                    self.engine._prefetch_sem.release()
                if media_bytes is not None:
                    sess.cache.put(i, media_bytes, sess.playhead)
                    if _init and sess.init is None:
                        with sess.lock:
                            if sess.init is None:
                                sess.init = _init
                    produced = True
                else:
                    # claim must never leak: on-demand waiters block on it
                    sess.cache.release(i)
                    if not self._stop.is_set():
                        errors = True
                # Pace the next fetch (0 unless the host has refused us). Only
                # once the claim is resolved: an on-demand request for this same
                # fragment is waiting on it.
                if sess.prefetch_gap and not self._stop.is_set():
                    self._stop.wait(timeout=sess.prefetch_gap)
            if refocus or self._stop.is_set():
                continue              # re-point without waiting
            if produced:
                idle_wait = 2.0       # healthy again -> normal cadence
            if throttled > 0:
                # sleep out the backoff the fetch path computed (Retry-After if
                # the host sent one). A seek wakes us; if that's early, the next
                # pass finds the deadline still in force and comes back here.
                self._wake.wait(timeout=min(throttled, 5.0))
                self._wake.clear()
            elif not produced:
                if errors:
                    # upstream is failing (dead link, expired token): back off
                    # instead of re-hitting it every 2s (a seek still resumes)
                    idle_wait = min(60.0, idle_wait * 2)
                self._wake.wait(timeout=idle_wait)
                self._wake.clear()
