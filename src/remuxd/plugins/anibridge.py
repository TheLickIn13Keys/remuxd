"""AniList -> playable media id via a local AniBridge instance.

AniBridge (https://anibridge.eliasbenb.dev) keeps the anibridge-mappings
dataset — ~19k AniList entries with episode-accurate links to IMDb/TMDB/TVDB —
in a local database and serves it at ``/api/mappings/<descriptor>``. We use it
as the first fallback when the AIOStreams anime api can't map an id: it knows
far more titles, and its ranges give us the *target's* episode number rather
than a guessed offset.

Note the dataset has no IMDb ids for series (only films), so series come back as
``tmdb:<id>:<s>:<e>`` / ``tvdb:...`` — AIOStreams searches those fine, and they
beat searching by bare AniList id (which usually returns nothing).

    ANIBRIDGE_URL        instance base url (default http://127.0.0.1:4848)
    ANIBRIDGE_USER/PASS  basic auth, if the instance has it enabled
    ANIBRIDGE_TIMEOUT    per-request seconds (default 5)
    ANIBRIDGE_CACHE_TTL  mapping memo seconds (default 3600; 0 = off)
    ANIBRIDGE_DISABLE    1/true/yes to skip this fallback
"""
import base64
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger("remuxd.resolve")

DEFAULT_URL = "http://127.0.0.1:4848"
_CACHE_MAX = 256
_DOWN_FOR = 60          # after a *timeout*, stop trying for this long
_MISS_TTL = 300         # "no mapping" is authoritative but cheap to re-ask

# "couldn't reach the instance", as distinct from "it has no mapping for this".
UNAVAILABLE = object()

# Which target the search likes best, in order. IMDb pulls the biggest stream
# pool, TMDB beats TVDB on results for series; tvdb_movie is last (it usually
# finds nothing, but it's better than no id at all).
_PREFERENCE = ("imdb_movie", "imdb_show", "tmdb_show", "tvdb_show",
               "tmdb_movie", "tvdb_movie")
_MOVIE_PROVIDERS = {"imdb_movie", "tmdb_movie", "tvdb_movie"}
_ID_PREFIX = {"tmdb_show": "tmdb:", "tvdb_show": "tvdb:",
              "tmdb_movie": "tmdb:", "tvdb_movie": "tvdb:"}

_cache = {}
_cache_lock = threading.Lock()
_down_until = 0.0


def _base() -> str:
    return (os.environ.get("ANIBRIDGE_URL") or DEFAULT_URL).rstrip("/")


def enabled() -> bool:
    return os.environ.get("ANIBRIDGE_DISABLE", "").lower() not in ("1", "true", "yes")


def _timeout() -> float:
    try:
        return float(os.environ.get("ANIBRIDGE_TIMEOUT", "") or 5)
    except ValueError:
        return 5.0


def _cache_ttl() -> int:
    try:
        return int(os.environ.get("ANIBRIDGE_CACHE_TTL", "") or 3600)
    except ValueError:
        return 3600


def _slow_failure(exc) -> bool:
    """True for a timeout (expensive to repeat), False for e.g. refused/DNS."""
    if isinstance(exc, TimeoutError):
        return True
    return isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, TimeoutError)


def _fetch(anilist_id):
    """The instance's mapping for an AniList id.

    None means it answered and has no mapping; UNAVAILABLE means we couldn't ask
    (down, still loading its database, timed out) — the caller must not cache
    that, or one blip would outlast the outage.
    """
    global _down_until
    if time.monotonic() < _down_until:
        return UNAVAILABLE
    url = f"{_base()}/api/mappings/{urllib.parse.quote(f'anilist:{anilist_id}')}"
    req = urllib.request.Request(url, headers={"User-Agent": "remuxd"})
    user, password = os.environ.get("ANIBRIDGE_USER"), os.environ.get("ANIBRIDGE_PASS")
    if user:
        tok = base64.b64encode(f"{user}:{password or ''}".encode()).decode()
        req.add_header("Authorization", f"Basic {tok}")
    try:
        with urllib.request.urlopen(req, timeout=_timeout()) as r:
            return json.load(r)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:                       # answered: simply not mapped
            return None
        log.debug("anibridge: %s -> HTTP %s", anilist_id, exc.code)
        return UNAVAILABLE
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        # A refused connection fails instantly, so just retry it next time: the
        # container may be a second away from ready. Only a *slow* failure earns
        # a back-off window, since paying that timeout per request would drag
        # every resolve down with it.
        if _slow_failure(exc):
            _down_until = time.monotonic() + _DOWN_FOR
            log.warning("anibridge: %s timed out (%s); skipping for %ds",
                        _base(), exc, _DOWN_FOR)
        else:
            log.debug("anibridge: %s unreachable (%s)", _base(), exc)
        return UNAVAILABLE


def _mapping(anilist_id):
    """_fetch, memoized for ANIBRIDGE_CACHE_TTL seconds.

    A "not mapped" answer is cached too (briefly), but a failure to reach the
    instance never is: otherwise a restart or a slow boot would keep answering
    "no mapping" for the whole TTL, and only restarting remuxd would clear it.
    """
    ttl, key = _cache_ttl(), str(anilist_id)
    if ttl <= 0:
        return _fetch(key)
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit:
            age, payload = now - hit[0], hit[1]
            if age <= (ttl if payload else min(ttl, _MISS_TTL)):
                return payload
    payload = _fetch(key)
    if payload is UNAVAILABLE:
        return payload
    with _cache_lock:
        if len(_cache) >= _CACHE_MAX and key not in _cache:
            _cache.pop(min(_cache, key=lambda k: _cache[k][0]), None)
        _cache[key] = (now, payload)
    return payload


def _map_episode(source_range, target_range, episode):
    """Translate an episode through one 'x[-y]' -> 'a[-b][,...][|ratio]' range.

    A positive ratio means each source episode spans n target episodes, a
    negative one that n source episodes share a target episode; we return the
    first target episode either way. None if the episode isn't in the range.
    """
    if not source_range or not target_range:
        return None
    start, dash, end = source_range.partition("-")
    try:                        # 'x' is one episode, 'x-' runs to infinity
        start = int(start)
        end = int(end) if end else (None if dash else start)
    except ValueError:
        return None
    if episode < start or (end is not None and episode > end):
        return None
    index = episode - start

    body, _, ratio_s = target_range.partition("|")
    if ratio_s:
        try:
            ratio = int(ratio_s)
        except ValueError:
            return None
        index = index * ratio if ratio > 0 else index // -ratio if ratio else index
    for seg in body.split(","):
        lo, dash, hi = seg.partition("-")
        try:
            lo = int(lo)
            hi = int(hi) if hi else (None if dash else lo)
        except ValueError:
            return None
        if hi is None:                            # open-ended: absorbs the rest
            return lo + index
        if index <= hi - lo:
            return lo + index
        index -= hi - lo + 1
    return None


def _target_episode(target, episode):
    """First target episode for this source episode, or None if unmapped."""
    for rng in target.get("ranges") or []:
        ep = _map_episode(rng.get("source_range"), rng.get("effective"), episode)
        if ep is not None:
            return ep
    return None


def _season(target):
    scope = (target.get("scope") or "").lower()
    if scope.startswith("s") and scope[1:].isdigit():
        return int(scope[1:])
    return None


def lookup(anilist_id, episode=1):
    """AniList id (+ episode) -> (media_id, media_type) for the stream search.

    (None, None) when the instance is unreachable, doesn't know the id, or has
    no target we can search by.
    """
    if not enabled():
        return None, None
    payload = _mapping(anilist_id)
    if not payload or payload is UNAVAILABLE:
        return None, None
    by_provider = {}
    for t in payload.get("targets") or []:
        if not t.get("deleted"):
            by_provider.setdefault(t.get("provider"), []).append(t)
    for provider in _PREFERENCE:
        for target in by_provider.get(provider, []):
            entry_id = target.get("entry_id")
            if not entry_id:
                continue
            prefix = _ID_PREFIX.get(provider, "")
            if provider in _MOVIE_PROVIDERS:
                # films carry a single "1" range; anything else isn't this movie
                if _target_episode(target, episode) is None:
                    continue
                return f"{prefix}{entry_id}", "movie"
            season, ep = _season(target), _target_episode(target, episode)
            if season is None or ep is None:
                continue
            return f"{prefix}{entry_id}:{season}:{ep}", "series"
    return None, None
