"""Multi-tenant session state.

The original demo kept one active stream in module globals; this replaces that
with a registry of independent sessions keyed by a short id. Each ``/start``
allocates a session; segment/subtitle/proxy requests look theirs up by id, so
many clients stream concurrently without clobbering each other. Idle sessions
are reaped on a timer and evicted when the concurrency cap is hit.
"""
import logging
import os
import shutil
import subprocess
import threading
import time
import uuid
from typing import Dict, List, Optional

log = logging.getLogger("remuxd.session")


class CapacityError(RuntimeError):
    """Session cap reached and every existing session is recently active."""


class FragmentCache:
    """Bounded in-memory cache of fMP4 media fragments (index -> bytes). Over budget
    it evicts the fragment farthest from the playhead, keeping a window (back-buffer
    + read-ahead) around the current position.

    In-flight coordination: exactly one producer works on a given index; everyone
    else waits on its Event rather than re-fetching (fetches are the expensive part
    on a slow CDN). ``claim`` hands out the produce/wait decision."""

    def __init__(self, budget_bytes: int):
        self.budget = budget_bytes
        self._data: Dict[int, bytes] = {}
        self._inflight: Dict[int, threading.Event] = {}
        self._lock = threading.Lock()
        self.bytes = 0

    def get(self, i: int) -> Optional[bytes]:
        with self._lock:
            return self._data.get(i)

    def has(self, i: int) -> bool:
        with self._lock:
            return i in self._data

    def claim(self, i: int):
        """-> (should_produce, event):
          (True, event)  caller should produce; event fires on put/release.
          (False, event) someone else is producing; wait on event, then re-get.
          (False, None)  already cached."""
        with self._lock:
            if i in self._data:
                return False, None
            ev = self._inflight.get(i)
            if ev is not None:
                return False, ev
            ev = threading.Event()
            self._inflight[i] = ev
            return True, ev

    def release(self, i: int) -> None:
        """Abandon a claim (production failed) and wake any waiters."""
        with self._lock:
            ev = self._inflight.pop(i, None)
        if ev is not None:
            ev.set()

    def put(self, i: int, data: bytes, playhead: int = 0) -> None:
        with self._lock:
            ev = self._inflight.pop(i, None)
            if i not in self._data:
                self._data[i] = data
                self.bytes += len(data)
                while self.bytes > self.budget and len(self._data) > 1:
                    # evict behind the playhead first (already watched), then the
                    # farthest ahead, never the leading edge we just prefetched.
                    far = max(self._data, key=lambda k: (k < playhead, abs(k - playhead)))
                    self.bytes -= len(self._data.pop(far))
        if ev is not None:
            ev.set()


class Session:
    """One live stream. ``kind`` selects which fields are meaningful:

      passthrough : url, headers                 (direct-play proxy)
      seek        : url, src, headers, windows,   (keyframe-indexed copy)
                    header_bytes, aac, amap, file_size, init (cached)
      singlepass  : proc                          (long-running ffmpeg -> HLS)
    """

    def __init__(self, sid: str, kind: str, out_dir: str):
        self.sid = sid
        self.kind = kind
        self.dir = out_dir
        self.tracks: List[dict] = []
        self.created = time.monotonic()
        self.last_access = self.created
        self.lock = threading.Lock()       # guards init caching / subs_started
        # separate from `lock`: refreshing blocks on a network round-trip, and
        # must not stall the request-path work `lock` protects
        self.url_lock = threading.Lock()

        # passthrough / seek
        self.url: Optional[str] = None
        self.src: Optional[str] = None
        self.headers: Optional[Dict[str, str]] = None
        # seek
        self.windows: Optional[List[dict]] = None
        self.header_bytes: Optional[bytes] = None
        self.aac: bool = True
        self.amap: str = "0:a:0?"
        self.file_size: Optional[int] = None
        self.init: Optional[bytes] = None
        # seek: read-ahead fragment cache + background prefetch worker + playhead
        self.cache: Optional[FragmentCache] = None
        self.prefetch = None            # engine.PrefetchWorker, set on start
        self.playhead: int = 0          # client's current segment index
        self.transcode: bool = False    # seek: re-encode each fragment vs copy
        # full-file subtitle/font extraction started (lazy: first /subs or
        # /fontlist hit triggers it, since it downloads the whole source)
        self.subs_started: bool = False
        # /subwindow results, (track, boff, bend) -> ASS bytes, plus in-flight
        # events under the same key. A window costs tens of MB of upstream fetch,
        # so seeking back over one must not re-pay it and concurrent callers
        # must share one extraction.
        self.subwin: Dict[tuple, bytes] = {}
        self.subwin_inflight: Dict[tuple, threading.Event] = {}
        self.subwin_lock = threading.Lock()
        # attachment streams from the probe (embedded fonts), for explicit dumping
        self.attachments: List[dict] = []
        # last debrid re-resolve (monotonic); throttles resolver lookups (429s)
        self.url_refreshed_at: float = 0.0
        # singlepass
        self.proc: Optional[subprocess.Popen] = None

    @property
    def on_demand(self) -> bool:
        """True for the per-segment seek paths (copy or transcode), where fragments
        are produced on request rather than by a long-running ffmpeg."""
        return self.windows is not None

    def touch(self) -> None:
        self.last_access = time.monotonic()

    def fonts_dir(self) -> str:
        return os.path.join(self.dir, "fonts")

    def close(self) -> None:
        """Terminate any ffmpeg and remove the working dir. Idempotent."""
        if self.prefetch:
            self.prefetch.stop()
        p = self.proc
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
        self.proc = None
        if self.dir and os.path.isdir(self.dir):
            shutil.rmtree(self.dir, ignore_errors=True)


class SessionManager:
    """Thread-safe registry of sessions with TTL reaping and a concurrency cap."""

    def __init__(self, root: str, ttl_seconds: int = 1800, max_sessions: int = 32,
                 min_evict_idle: float = 60.0):
        self.root = root
        self.ttl = ttl_seconds
        self.max_sessions = max_sessions
        # only sessions idle at least this long may be evicted to make room; if
        # every session is younger, create() raises CapacityError instead of
        # killing someone's live stream.
        self.min_evict_idle = min_evict_idle
        self._sessions: Dict[str, Session] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        os.makedirs(root, exist_ok=True)
        self._reaper = threading.Thread(target=self._reap_loop, name="remuxd-reaper",
                                        daemon=True)
        self._reaper.start()

    def create(self, kind: str) -> Session:
        """Allocate a new session (and its working dir). If the concurrency cap is
        hit, evicts the least-recently-used *idle* session; raises CapacityError
        when every session is recently active (callers answer 503)."""
        sid = uuid.uuid4().hex[:12]
        out_dir = os.path.join(self.root, sid)
        os.makedirs(out_dir, exist_ok=True)
        sess = Session(sid, kind, out_dir)
        evicted = None
        with self._lock:
            if self.max_sessions and len(self._sessions) >= self.max_sessions:
                now = time.monotonic()
                idle = [s for s in self._sessions.values()
                        if now - s.last_access >= self.min_evict_idle]
                if not idle:
                    raise CapacityError(
                        f"at capacity ({self.max_sessions} active sessions)")
                oldest = min(idle, key=lambda s: s.last_access)
                evicted = self._sessions.pop(oldest.sid, None)
            self._sessions[sid] = sess
        if evicted:
            log.info("evicting idle session %s (cap %d reached)",
                     evicted.sid, self.max_sessions)
            evicted.close()
        log.info("session %s created (%s)", sid, kind)
        return sess

    def get(self, sid: str) -> Optional[Session]:
        with self._lock:
            sess = self._sessions.get(sid)
        if sess:
            sess.touch()
        return sess

    def remove(self, sid: str) -> None:
        with self._lock:
            sess = self._sessions.pop(sid, None)
        if sess:
            sess.close()
            log.info("session %s removed", sid)

    def _reap_loop(self) -> None:
        while not self._stop.wait(min(60, max(1, self.ttl // 4))):
            now = time.monotonic()
            with self._lock:
                dead = [s for s in self._sessions.values()
                        if now - s.last_access > self.ttl]
                for s in dead:
                    self._sessions.pop(s.sid, None)
            for s in dead:
                log.info("reaping idle session %s", s.sid)
                s.close()

    def shutdown(self) -> None:
        """Stop the reaper and tear down every session. Only removes the root dir
        if it's empty afterwards: the root may be a pre-existing directory the
        user pointed us at (REMUXD_SESSION_ROOT=/tmp would be catastrophic to
        rmtree)."""
        self._stop.set()
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for s in sessions:
            s.close()
        try:
            os.rmdir(self.root)
        except OSError:
            pass   # not empty / already gone; leave it alone
