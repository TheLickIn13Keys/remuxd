"""Offline AniList -> IMDb fallback, from the Kometa Anime-IDs dataset.

The AIOStreams anime API is the primary mapper; it misses plenty of titles and
returns nothing at all when the instance is down. This is the fallback: one
~1.7 MB JSON snapshot of https://github.com/Kometa-Team/Anime-IDs, keyed by
AniDB id, re-indexed here by AniList id and memoized on disk so a restart
doesn't refetch. Only ~1600 of its entries carry an IMDb id (mostly films), so
treat it as a bonus lookup, not a replacement.

    ANIME_IDS_URL      override the snapshot url
    ANIME_IDS_CACHE    snapshot path (default: <tmp>/remuxd-anime-ids.json)
    ANIME_IDS_TTL      seconds before the snapshot is refetched (default 86400)
    ANIME_IDS_DISABLE  set to 1/true/yes to turn the fallback off
"""
import json
import logging
import os
import tempfile
import threading
import time
import urllib.request

log = logging.getLogger("remuxd.resolve")

DEFAULT_URL = ("https://raw.githubusercontent.com/Kometa-Team/Anime-IDs/"
               "master/anime_ids.json")
DEFAULT_TTL = 24 * 60 * 60
_RETRY_AFTER = 300          # don't refetch a failed download on every request

_index = None               # anilist id (str) -> entry dict
_index_mtime = 0.0          # monotonic time the in-memory index was built
_failed_at = 0.0            # monotonic time of the last failed load
_lock = threading.Lock()


def _ttl() -> int:
    try:
        return int(os.environ.get("ANIME_IDS_TTL", "") or DEFAULT_TTL)
    except ValueError:
        return DEFAULT_TTL


def _cache_path() -> str:
    return os.environ.get("ANIME_IDS_CACHE") or os.path.join(
        tempfile.gettempdir(), "remuxd-anime-ids.json")


def enabled() -> bool:
    return os.environ.get("ANIME_IDS_DISABLE", "").lower() not in ("1", "true", "yes")


def _download() -> bytes:
    url = os.environ.get("ANIME_IDS_URL") or DEFAULT_URL
    req = urllib.request.Request(url, headers={"User-Agent": "remuxd"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def _snapshot() -> dict:
    """The raw dataset: fresh disk copy, else a download, else a stale disk copy."""
    path, ttl = _cache_path(), _ttl()
    try:
        age = time.time() - os.path.getmtime(path)
    except OSError:
        age = None
    if age is not None and (ttl <= 0 or age < ttl):
        with open(path, "rb") as f:
            return json.load(f)
    try:
        raw = _download()
        data = json.loads(raw)
        try:                                     # cache is best-effort
            tmp = f"{path}.{os.getpid()}.tmp"
            with open(tmp, "wb") as f:
                f.write(raw)
            os.replace(tmp, path)
        except OSError as exc:
            log.debug("anime-ids: cannot write %s (%s)", path, exc)
        return data
    except Exception as exc:
        if age is None:
            raise
        log.warning("anime-ids: refresh failed (%s); using cached copy", exc)
        with open(path, "rb") as f:
            return json.load(f)


def _build() -> dict:
    """Re-key the AniDB-keyed dataset by AniList id, keeping only IMDb-bearing rows."""
    idx = {}
    for entry in _snapshot().values():
        imdb, anilist = entry.get("imdb_id"), entry.get("anilist_id")
        if not imdb or not anilist:
            continue
        for aid in str(anilist).split(","):      # schema allows a comma list
            aid = aid.strip()
            if aid:
                idx.setdefault(aid, entry)
    return idx


def _get_index():
    """Cached index, or None when the dataset can't be loaded right now."""
    global _index, _index_mtime, _failed_at
    if not enabled():
        return None
    with _lock:
        ttl, now = _ttl(), time.monotonic()
        if _index is not None and (ttl <= 0 or now - _index_mtime < ttl):
            return _index
        if _failed_at and now - _failed_at < _RETRY_AFTER:
            return _index                        # stale index or None; no retry storm
        try:
            _index = _build()
            _index_mtime, _failed_at = now, 0.0
            log.info("anime-ids: indexed %d anilist->imdb mappings", len(_index))
        except Exception as exc:
            _failed_at = now
            log.warning("anime-ids: load failed (%s); fallback disabled for %ds",
                        exc, _RETRY_AFTER)
        return _index


def lookup(anilist_id):
    """AniList id -> (imdb_id, is_movie, season, from_episode).

    ``is_movie`` is a guess from the shape of the row: entries with a TVDb
    season >= 1 are series episodes, everything else maps straight to a film's
    IMDb title. All-None when the id isn't in the dataset.
    """
    idx = _get_index()
    entry = idx.get(str(anilist_id).strip()) if idx else None
    if not entry:
        return None, None, None, None
    imdb = str(entry["imdb_id"]).split(",")[0].strip()   # first of a comma list
    season = entry.get("tvdb_season")
    if not isinstance(season, int) or season < 1:
        return imdb, True, None, None                    # film
    offset = entry.get("tvdb_epoffset")
    from_episode = offset + 1 if isinstance(offset, int) else None
    return imdb, False, season, from_episode
