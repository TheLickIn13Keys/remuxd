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
    need a re-resolve — which fragment() already handles."""
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
            # repeat /start on this src (audio switch, replay) — skip resolve+probe
            final, fsize, probed = entry.final, entry.fsize, entry.probed
            tl.lap("prep_cache")
        else:
            # Resolve the debrid/redirect URL to the real CDN link ONCE — these re-run
            # a lookup per hit and 429 if touched repeatedly. Capture the file size from
            # the same response (Content-Range) instead of probing it again later.
            final, fsize = resolve_final(src, cfg.user_agent, headers)
            tl.lap("resolve_url")
            probed = media.probe(cfg.ffprobe, final, headers)
            tl.lap("probe")
            entry = _PrepEntry(final, fsize, probed, time.monotonic())
            self._prep_put(key, entry)
        v, pf, a, cont, subs, aidx, audios, _dur = probed
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
        v, pf, a, cont, subs, aidx, audios, dur = probed
        if dur <= 0:
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
            windows = mkvcues.segment_grid(plan, dur, cfg.segment_seconds)
            if not windows:
                return None
            header_bytes = mkvcues.fetch_range(final, 0, plan["header_size"], headers)
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
        sess.tracks = subtitles.sub_tracks(subs, sess.dir)
        sess.cache = FragmentCache(cfg.fragment_cache_bytes)
        if cfg.prefetch_segments > 0:
            sess.prefetch = PrefetchWorker(self, sess, cfg.prefetch_segments)
            sess.prefetch.start()
        self._spawn_sub_extraction(sess, final, headers)
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
                                        ffprobe=cfg.ffprobe, venc=cfg.video_encode_args)
        sess.tracks = subtitles.sub_tracks(info.get("subs_streams") or [], sess.dir)
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

        self._spawn_sub_extraction(sess, final, headers)
        if tl:
            tl.done(f"singlepass sid={sess.sid}")
        return self._info(
            sess, info["video"], info["pix_fmt"], info["audio"], info["container"],
            info["mode"], info["video_action"], info["audio_action"],
            playlist=f"/hls/{sess.sid}/index.m3u8",
            tracks=sess.tracks, audio_meta=audio_meta)

    def fragment(self, sess: Session, i: int):
        """Produce fragment i: fetch its cluster bytes (+ MKV header) via Range and
        remux to fMP4. Returns (init, media_bytes, err); re-resolves the link once
        if a byte fetch fails."""
        w = sess.windows[i]

        def build_blob(url):
            return sess.header_bytes + mkvcues.fetch_range(url, w["boff"], w["bend"], sess.headers)

        t0 = time.perf_counter()
        try:
            blob = build_blob(sess.url)
        except Exception:
            sess.url = resolve_final_url(sess.src, self.cfg.user_agent, sess.headers)
            blob = build_blob(sess.url)
        t_fetch = (time.perf_counter() - t0) * 1000

        t1 = time.perf_counter()
        p = subprocess.Popen(media.segment_transcode_cmd(self.cfg.ffmpeg, self.cfg.video_encode_args,
                                                         sess.aac, sess.amap)
                             if sess.transcode
                             else media.segment_cmd(self.cfg.ffmpeg, sess.aac, sess.amap),
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE)
        try:
            out, err = p.communicate(blob, timeout=180)
        except subprocess.TimeoutExpired:
            p.kill()
            p.communicate()      # reap; don't leave a hung ffmpeg holding a slot
            log.warning("[seg %d] ffmpeg timed out", i)
            return None, None, b"ffmpeg timeout"
        t_ff = (time.perf_counter() - t1) * 1000
        if not out:
            log.warning("[seg %d] empty window=%.2f-%.2f bytes[%s:%s] stderr=%s",
                        i, w["start"], w["end"], w["boff"], w["bend"],
                        err.decode(errors="replace").strip()[-500:])
            return None, None, err
        init, media_bytes = media.split_init(out)
        log.debug("[frag %s#%d] fetch=%.0fms(%dKB) %s=%.0fms out=%dKB",
                  sess.sid, i, t_fetch, len(blob) // 1024,
                  "encode" if sess.transcode else "remux", t_ff, len(out) // 1024)
        return init, media_bytes, None

    def serve_segment(self, sess: Session, i: int) -> Optional[bytes]:
        """Fragment i for a seek session — from the read-ahead cache if warm, from
        an in-flight producer if one is already fetching it (wait, don't re-fetch),
        else produced on demand. Moves the playhead so the worker reads ahead."""
        if i < 0 or i >= len(sess.windows):
            return None
        if sess.prefetch:
            sess.prefetch.notify(i)
        if not sess.cache:
            _init, media_bytes, _ = self.fragment(sess, i)
            return media_bytes

        cached = sess.cache.get(i)
        if cached is not None:
            log.debug("[seg %s#%d] cache-hit (%dKB)", sess.sid, i, len(cached) // 1024)
            return cached
        should, ev = sess.cache.claim(i)
        if not should:
            # someone else (usually the prefetch worker) is already fetching it —
            # wait for their result instead of re-downloading the same bytes.
            if ev is not None and ev.wait(timeout=120):
                cached = sess.cache.get(i)
                if cached is not None:
                    log.debug("[seg %s#%d] in-flight-hit (%dKB)", sess.sid, i, len(cached) // 1024)
                    return cached
            sess.cache.claim(i)   # producer failed/evicted -> take over

        init, media_bytes, _err = self.fragment(sess, i)
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
        cache, so it shares the fetch with prefetch instead of duplicating it —
        serve_segment sets sess.init as a side effect."""
        if sess.init is None:
            self.serve_segment(sess, 0)
        return sess.init

    def subwindow(self, sess: Session, n: int, t: float) -> bytes:
        """On-demand ASS around time ``t``: extract track n from the clusters
        spanning ~[t-10, t+45] with -copyts (preserve absolute timestamps).
        Re-resolves the link once if the fetch comes back empty."""
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
        idx = sess.tracks[n]["index"]

        def extract(url):
            return subtitles.extract_window(self.cfg.ffmpeg, url, sess.header_bytes,
                                            boff, bend, idx, sess.headers)

        try:
            out = extract(sess.url)
        except Exception:
            out = b""
        if not out:   # link may have expired -> re-resolve once
            sess.url = resolve_final_url(sess.src, self.cfg.user_agent, sess.headers)
            out = extract(sess.url)
        return out

    def _spawn_sub_extraction(self, sess: Session, url: str, headers) -> None:
        if not sess.tracks:
            return
        threading.Thread(
            target=subtitles.extract_all,
            args=(self.cfg.ffmpeg, url, sess.tracks, sess.fonts_dir(), headers),
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
    """

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
        while not self._stop.is_set():
            produced = refocus = False
            ph = sess.playhead
            hi = min(n, ph + 1 + self.read_ahead)
            for i in range(ph, hi):
                if self._stop.is_set():
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
                try:
                    if self._stop.is_set():
                        break
                    _init, media_bytes, _err = self.engine.fragment(sess, i)
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
                    sess.cache.release(i)
            if refocus or self._stop.is_set():
                continue              # re-point without waiting
            if not produced:
                self._wake.wait(timeout=2.0)
                self._wake.clear()
