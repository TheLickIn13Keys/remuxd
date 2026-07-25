"""AniList -> playable stream resolver, via an AIOStreams instance.

Maps an AniList id to an IMDb id (richer stream pool), searches an AIOStreams
instance, and ranks the results for *browser playability* (copy-friendly codecs
and instant/cached sources first). Returns the ranked list for ``/resolve``.
When the instance's anime api can't map the id, we retry against a local
``anibridge`` instance (IMDb/TMDB/TVDB, episode-accurate) and then the offline
Kometa ``animeids`` snapshot; failing both we search by AniList id directly.

Configuration is env-only (no baked-in credentials):
    AIO_INSTANCE   e.g. https://aiostreams.example.com
    AIO_UUID       your instance uuid
    AIO_PASS       your instance password
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

from ..timing import Timeline
from . import anibridge, animeids

log = logging.getLogger("remuxd.resolve")

# TTL memo of resolve results, so re-opening / re-picking a title skips the slow
# debrid search. Keyed by the request token; env AIO_CACHE_TTL seconds (0 = off).
_cache = {}
_cache_lock = threading.Lock()
_CACHE_MAX = 64


def _cache_ttl() -> int:
    try:
        return int(os.environ.get("AIO_CACHE_TTL", "300"))
    except ValueError:
        return 300

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36")

COPY_VIDEO = {"HEVC", "H.265", "X265", "AVC", "H.264", "X264"}
COPY_AUDIO = {"AAC", "MP3"}
BITRATE_CEIL = 10_000_000
PREFERRED_GROUPS = {"TOONSHUB": 60}


def _instance() -> str:
    return os.environ.get("AIO_INSTANCE", "").rstrip("/")


def configured() -> bool:
    return bool(_instance() and os.environ.get("AIO_UUID") and os.environ.get("AIO_PASS"))


def _http_json(url: str, auth: bool = False):
    req = urllib.request.Request(url)
    req.add_header("User-Agent", _UA)
    if auth:
        tok = base64.b64encode(
            f"{os.environ['AIO_UUID']}:{os.environ['AIO_PASS']}".encode()).decode()
        req.add_header("Authorization", f"Basic {tok}")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def _map_to_imdb(anilist_id):
    """AniList id -> (imdb_id, is_movie, imdb_season, from_episode).

    Any field may be None when the api doesn't know it; ``is_movie`` is None
    (rather than False) when the api gave us no type at all, so the caller can
    tell "not a movie" apart from "unknown".
    """
    q = urllib.parse.urlencode({"idType": "anilistId", "idValue": anilist_id})
    try:
        data = _http_json(f"{_instance()}/api/v1/anime?{q}").get("data")
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        log.debug("[resolve] anime api lookup failed for %s: %s", anilist_id, exc)
        return None, None, None, None
    if not data:
        return None, None, None, None
    mappings = data.get("mappings") or {}
    imdb = mappings.get("imdbId") or data.get("imdbId") or data.get("imdb_id")
    atype = (data.get("type") or "").upper()
    is_movie = atype in ("MOVIE", "FILM") if atype else None
    imdb_block = data.get("imdb") or {}
    return imdb, is_movie, imdb_block.get("seasonNumber"), imdb_block.get("fromEpisode")


def _search(media_id, media_type="series"):
    q = urllib.parse.urlencode({"type": media_type, "id": media_id})
    resp = _http_json(f"{_instance()}/api/v1/search?{q}", auth=True)
    if not resp.get("success"):
        raise RuntimeError(resp.get("error"))
    return resp["data"]["results"]


def _decide(r):
    """(video_action, audio_action, is_playable, notes) for a result."""
    p = r.get("parsedFile") or {}
    enc = (p.get("encode") or "").upper()
    vis = [v.upper() for v in (p.get("visualTags") or [])]
    aud = [a.upper() for a in (p.get("audioTags") or [])]
    notes = []
    is_avc = enc in ("AVC", "H.264", "X264")
    if is_avc and "10BIT" in vis:
        return ("transcode->h264", "transcode->aac", False, ["Hi10 (10-bit AVC): must transcode"])
    if any(t in vis for t in ("DV", "HDR", "HDR10", "HDR10+", "HLG")):
        notes.append("HDR/DV: transcode (tone-map)")
        video = "transcode->h264"
    elif enc in COPY_VIDEO:
        video = f"copy ({enc})"
    else:
        video = f"transcode->h264 (source {enc or '?'})"
        notes.append(f"codec {enc or '?'} not copyable")
    if aud and any(a in COPY_AUDIO for a in aud):
        audio = "copy (AAC)"
    elif not aud:
        audio = "copy? (audio tag unknown)"
    else:
        audio = f"transcode->aac ({'/'.join(aud)})"
    return (video, audio, True, notes)


def _is_instant(r):
    return bool(r.get("cached")) or (r.get("type") or "").lower() in (
        "http", "external", "live", "youtube")


def _score(r, decision=None):
    """Higher = better for browser playability. Pass a precomputed _decide() result
    to avoid recomputing it."""
    p = r.get("parsedFile") or {}
    langs = [l.lower() for l in (p.get("languages") or [])]
    subs = [s.lower() for s in (p.get("subtitles") or [])]
    br = r.get("bitrate") or (p.get("bitrate") or 0)
    video, audio, playable, _ = decision or _decide(r)
    s = 0
    if not playable:
        s -= 1000
    if video.startswith("copy"):
        s += 300
    if audio.startswith("copy ("):
        s += 120
    elif audio.startswith("copy?"):
        s += 40
    if _is_instant(r):
        s += 200
    if r.get("seadexBest"):
        s += 15
    s += PREFERRED_GROUPS.get((p.get("releaseGroup") or "").upper(), 0)
    if p.get("resolution") == "1080p":
        s += 40
    if any("english" in l or "dual" in l for l in langs):
        s += 25
    if any("english" in x for x in subs):
        s += 15
    if br:
        if br > BITRATE_CEIL:
            s -= int((br - BITRATE_CEIL) / 1_000_000) * 15
        else:
            s += 10
    return s


def _imdb_media_id(imdb, imdb_season, from_episode, cli_season, episode):
    season = imdb_season if imdb_season else cli_season
    ep = (from_episode or 1) + (episode - 1)
    return f"{imdb}:{season}:{ep}"


def parse_id(token: str):
    """'108465' -> ep1; '108465:5' -> ep5; '108465:2:5' -> s2e5."""
    if ":" in token:
        parts = token.split(":")
        anilist = parts[0]
        if len(parts) == 2:
            return anilist, 1, int(parts[1])
        s = int(parts[1]) if len(parts) > 1 and parts[1] else 1
        e = int(parts[2]) if len(parts) > 2 and parts[2] else 1
        return anilist, s, e
    return token, 1, 1


def _fallback_media(anilist, season, episode, is_movie):
    """(media_id, media_type) from the offline mappers, or (None, None).

    AniBridge first (episode-accurate, knows the most titles), then the Kometa
    Anime-IDs snapshot, which still covers a few hundred films AniBridge lacks.
    """
    media_id, media_type = anibridge.lookup(anilist, episode)
    if media_id:
        log.info("[resolve] anibridge fallback: anilist %s ep %s -> %s",
                 anilist, episode, media_id)
        return media_id, media_type
    imdb, kometa_movie, imdb_season, from_episode = animeids.lookup(anilist)
    if imdb:
        log.info("[resolve] anime-ids fallback: anilist %s -> %s", anilist, imdb)
        movie = kometa_movie if is_movie is None else is_movie
        if movie:
            return imdb, "movie"
        return _imdb_media_id(imdb, imdb_season, from_episode, season, episode), "series"
    return None, None


def _resolve_streams(anilist, season=1, episode=1, tl=None):
    media_id, media_type = None, "series"
    imdb, is_movie, imdb_season, from_episode = _map_to_imdb(anilist)
    if tl:
        tl.lap("map_imdb")
    if imdb:
        media_id = imdb if is_movie else \
            _imdb_media_id(imdb, imdb_season, from_episode, season, episode)
        media_type = "movie" if is_movie else "series"
    else:                        # api couldn't map it (or is down): offline mappers
        media_id, media_type = _fallback_media(anilist, season, episode, is_movie)
        if tl:
            tl.lap("fallback")
    if not media_id:
        media_id, media_type = f"anilist:{anilist}:{episode}", "series"
    results = _search(media_id, media_type)
    if tl:
        tl.lap("search")
    # decide + score each result exactly once, then sort on the stored score
    for r in results:
        v, a, playable, notes = _decide(r)
        r["_decision"] = {"video": v, "audio": a, "playable": playable, "notes": notes}
        r["_score"] = _score(r, (v, a, playable, notes))
    ranked = sorted(results, key=lambda r: r["_score"], reverse=True)
    if tl:
        tl.lap("rank")
    return media_id, ranked


def resolve_api(anilist_token: str) -> dict:
    """Resolver entrypoint used by the server's /resolve endpoint."""
    token = anilist_token.strip()
    ttl = _cache_ttl()
    if ttl > 0:
        with _cache_lock:
            hit = _cache.get(token)
            if hit and time.monotonic() - hit[0] <= ttl:
                log.debug("[resolve:%s] cache-hit", token)
                return hit[1]
    tl = Timeline(log, "resolve:" + token)
    aid, s, e = parse_id(token)
    media_id, ranked = _resolve_streams(aid, s, e, tl=tl)
    results = [{
        "filename": r.get("filename"),
        "url": r.get("url"),
        "group": (r.get("parsedFile") or {}).get("releaseGroup"),
        "encode": (r.get("parsedFile") or {}).get("encode"),
        "visualTags": (r.get("parsedFile") or {}).get("visualTags"),
        "audioTags": (r.get("parsedFile") or {}).get("audioTags"),
        "bitrate": r.get("bitrate"),
        "cached": r.get("cached"),
        "type": r.get("type"), "service": r.get("service"),
        "decision": r.get("_decision"), "score": r.get("_score"),
    } for r in ranked]
    tl.done(f"{media_id} results={len(results)}")
    result = {"media_id": media_id, "results": results}
    # Never memoize an empty search: it usually means something upstream was
    # briefly unhappy (a mapper still booting, a debrid timeout), and caching it
    # turns one bad click into TTL seconds of "0 streams" that only a restart clears.
    if ttl > 0 and results:
        with _cache_lock:
            if len(_cache) >= _CACHE_MAX and token not in _cache:
                _cache.pop(min(_cache, key=lambda k: _cache[k][0]), None)
            _cache[token] = (time.monotonic(), result)
    return result
