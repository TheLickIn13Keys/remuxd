#!/usr/bin/env python3
"""
mkvcues.py: get keyframe (time, byte-offset) pairs from an MKV/WebM Cues index
using only small HTTP byte-range reads (no full download).

Matroska stores a seek index (Cues) whose position is advertised in the
SeekHead near the file start. So two ranged reads suffice:
  1. ~64KB header  -> EBML header + Segment start + SeekHead (-> Cues offset)
  2. the Cues bytes -> CuePoints -> (CueTime, CueClusterPosition) pairs

Timestamps come out in seconds; offsets are absolute file byte positions of the
cluster that starts each keyframe GOP. Element IDs are per the Matroska spec.

Public API:
  keyframe_times(url, headers, file_size) -> sorted list[float] | None
    None means "no usable Cues" (caller should fall back to a full-file path).
"""
from . import netio

# --- Matroska/EBML element IDs (from the spec) -----------------------------
SEGMENT       = 0x18538067
SEEK_HEAD     = 0x114D9B74
SEEK          = 0x4DBB
SEEK_ID       = 0x53AB
SEEK_POSITION = 0x53AC
CUES          = 0x1C53BB6B
CUE_POINT     = 0xBB
CUE_TIME      = 0xB3
CUE_TRACK_POS = 0xB7
CUE_TRACK     = 0xF7
CUE_CLUSTER_POSITION = 0xF1
TRACKS        = 0x1654AE6B
TRACK_ENTRY   = 0xAE
TRACK_NUMBER  = 0xD7
TRACK_TYPE    = 0x83
CODEC_PRIVATE = 0x63A2
INFO          = 0x1549A966
ATTACHMENTS   = 0x1941A469
CLUSTER       = 0x1F43B675
TIMESTAMP_SCALE = 0x2AD7B1
EBML_HEADER   = 0x1A45DFA3

HEADER_PROBE = 64 * 1024
MAX_CUES     = 4 * 1024 * 1024
# An element this large inside the header region is malformed (or an EBML
# "unknown size" marker, which decodes as a huge value), so it can't be skipped over.
_MAX_ELEMENT = 1 << 40
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120 Safari/537.36")


def _get_range(url, start, length, headers=None):
    return netio.fetch_range(url, start, start + length, UA, headers, timeout=30)


# --- EBML primitives -------------------------------------------------------
def _read_id(buf, pos):
    """Element ID: length from leading byte, MARKER RETAINED. -> (id, newpos)."""
    b0 = buf[pos]
    mask = 0x80
    n = 1
    while n <= 4 and not (b0 & mask):
        mask >>= 1
        n += 1
    if n > 4:
        raise ValueError("bad EBML id")
    val = 0
    for i in range(n):
        val = (val << 8) | buf[pos + i]
    return val, pos + n


def _read_size(buf, pos):
    """Element size: length from leading byte, marker STRIPPED. -> (size, newpos)."""
    b0 = buf[pos]
    mask = 0x80
    n = 1
    while n <= 8 and not (b0 & mask):
        mask >>= 1
        n += 1
    if n > 8:
        raise ValueError("bad EBML size")
    val = b0 & (mask - 1)
    for i in range(1, n):
        val = (val << 8) | buf[pos + i]
    return val, pos + n


def _read_uint(buf, pos, size):
    v = 0
    for i in range(size):
        v = (v << 8) | buf[pos + i]
    return v


def parse_ebml_header(buf):
    """-> byte offset where the Segment's children begin (segment data offset)."""
    pos = 0
    eid, pos = _read_id(buf, pos)
    if eid != EBML_HEADER:
        raise ValueError("not an EBML/Matroska file")
    size, pos = _read_size(buf, pos)
    pos += size                                   # skip EBML header body
    eid, pos = _read_id(buf, pos)
    if eid != SEGMENT:
        raise ValueError("Segment not found after header")
    _size, pos = _read_size(buf, pos)             # Segment data starts here
    return pos


def parse_seek_head(buf, seg_off):
    """Walk Segment children for a SeekHead; return {element_id: relative_offset}."""
    positions = {}
    pos = seg_off
    end = len(buf)
    while pos < end - 2:
        try:
            eid, p = _read_id(buf, pos)
            size, p = _read_size(buf, p)
        except (IndexError, ValueError):
            break
        if eid == SEEK_HEAD:
            positions.update(_parse_seeks(buf, p, p + size))
            # keep scanning: some files have multiple SeekHeads
        elif eid == CLUSTER:                      # Cluster -> we've gone too far
            break
        if size >= _MAX_ELEMENT:                  # unknown size -> can't skip safely
            break
        pos = p + size
    return positions


def _parse_seeks(buf, pos, end):
    out = {}
    while pos < end - 1:
        eid, p = _read_id(buf, pos)
        size, p = _read_size(buf, p)
        if eid == SEEK:
            sid = spos = None
            q, qend = p, p + size
            while q < qend - 1:
                e2, q2 = _read_id(buf, q)
                s2, q2 = _read_size(buf, q2)
                if e2 == SEEK_ID:
                    sid = _read_uint(buf, q2, s2)
                elif e2 == SEEK_POSITION:
                    spos = _read_uint(buf, q2, s2)
                q = q2 + s2
            if sid is not None and spos is not None:
                out[sid] = spos
        pos = p + size
    return out


def _timestamp_scale(buf, seg_off):
    """Read Info/TimestampScale (ns per tick); default 1,000,000 (=> ms ticks)."""
    pos = seg_off
    end = len(buf)
    while pos < end - 2:
        try:
            eid, p = _read_id(buf, pos)
            size, p = _read_size(buf, p)
        except (IndexError, ValueError):
            break
        if eid == INFO:
            q, qend = p, p + size
            while q < qend - 1:
                e2, q2 = _read_id(buf, q)
                s2, q2 = _read_size(buf, q2)
                if e2 == TIMESTAMP_SCALE:
                    return _read_uint(buf, q2, s2)
                q = q2 + s2
        if size >= _MAX_ELEMENT:
            break
        pos = p + size
    return 1_000_000


def _video_track_number(buf, seg_off):
    """TrackNumber of the first video track (TrackType 1), or None. Cues can
    index several tracks, and audio/subtitle cue positions point at clusters
    far from the video keyframe, so windows must be built from the video track's
    positions only."""
    pos = seg_off
    end = len(buf)
    while pos < end - 2:
        try:
            eid, p = _read_id(buf, pos)
            size, p = _read_size(buf, p)
        except (IndexError, ValueError):
            break
        if eid == TRACKS:
            q, qend = p, min(p + size, end)
            while q < qend - 1:
                try:
                    e2, q2 = _read_id(buf, q)
                    s2, q2 = _read_size(buf, q2)
                except (IndexError, ValueError):
                    return None
                if e2 == TRACK_ENTRY:
                    num = ttype = None
                    r, rend = q2, min(q2 + s2, end)
                    while r < rend - 1:
                        e3, r2 = _read_id(buf, r)
                        s3, r2 = _read_size(buf, r2)
                        if e3 == TRACK_NUMBER:
                            num = _read_uint(buf, r2, s3)
                        elif e3 == TRACK_TYPE:
                            ttype = _read_uint(buf, r2, s3)
                        r = r2 + s3
                    if ttype == 1 and num is not None:
                        return num
                q = q2 + s2
        elif eid == CLUSTER:
            break
        if size >= _MAX_ELEMENT:
            break
        pos = p + size
    return None


def strip_attachments(header: bytes, seg_off: int) -> bytes:
    """Remove Attachments elements from a header blob (bytes [0, first cluster)).
    Embedded fonts can be tens of MB, and the header is prepended to every fragment
    piped to ffmpeg, and the A/V remux never needs them (fonts are served to the
    client out-of-band). SeekHead offsets go stale, but ffmpeg ignores them on
    non-seekable (piped) input. Returns the header unchanged if anything looks off."""
    spans = []
    pos = seg_off
    end = len(header)
    while pos < end - 2:
        try:
            eid, p = _read_id(header, pos)
            size, p = _read_size(header, p)
        except (IndexError, ValueError):
            break
        if size >= _MAX_ELEMENT or p + size > end:   # malformed/truncated: keep as-is
            break
        if eid == ATTACHMENTS:
            spans.append((pos, p + size))
        elif eid == CLUSTER:
            break
        pos = p + size
    if not spans:
        return header
    parts, prev = [], 0
    for s, e in spans:
        parts.append(header[prev:s])
        prev = e
    parts.append(header[prev:])
    return b"".join(parts)


def _fetch_toplevel(url, header, seg_off, target_id, headers=None):
    """Locate a top-level Segment child by element id and return its BODY bytes,
    or None. Prefers the SeekHead offset; falls back to walking element headers
    (12 bytes each) up to the first Cluster. Ranged reads only."""
    seeks = parse_seek_head(header, seg_off)
    abs_off = seg_off + seeks[target_id] if target_id in seeks else None
    if abs_off is None:
        pos = seg_off
        for _ in range(64):
            head = (header[pos:pos + 12] if pos + 12 <= len(header)
                    else fetch_range(url, pos, pos + 12, headers))
            try:
                eid, p = _read_id(head, 0)
                size, p = _read_size(head, p)
            except (IndexError, ValueError):
                return None
            if eid == target_id:
                abs_off = pos
                break
            if eid == CLUSTER or size >= _MAX_ELEMENT:
                return None
            pos += p + size
        if abs_off is None:
            return None
    head = (header[abs_off:abs_off + 12] if abs_off + 12 <= len(header)
            else fetch_range(url, abs_off, abs_off + 12, headers))
    eid, p = _read_id(head, 0)
    if eid != target_id:
        return None
    size, p = _read_size(head, p)
    if size >= _MAX_ELEMENT:
        return None
    body_abs = abs_off + p
    if body_abs + size <= len(header):
        return header[body_abs:body_abs + size]
    return fetch_range(url, body_abs, body_abs + size, headers)


def fetch_subtitle_privates(url, headers=None):
    """CodecPrivate blobs of the subtitle tracks (TrackType 17), via ranged
    reads. For S_TEXT/ASS tracks the blob is the ASS script header, including
    [V4+ Styles] with the font names each style renders with."""
    header = _get_range(url, 0, HEADER_PROBE, headers)
    seg_off = parse_ebml_header(header)
    body = _fetch_toplevel(url, header, seg_off, TRACKS, headers)
    if not body:
        return []
    out = []
    pos, end = 0, len(body)
    while pos < end - 1:
        try:
            eid, p = _read_id(body, pos)
            size, p = _read_size(body, p)
        except (IndexError, ValueError):
            break
        if size >= _MAX_ELEMENT or p + size > end:
            break
        if eid == TRACK_ENTRY:
            ttype = priv = None
            q, qend = p, p + size
            while q < qend - 1:
                try:
                    e2, q2 = _read_id(body, q)
                    s2, q2 = _read_size(body, q2)
                except (IndexError, ValueError):
                    break
                if q2 + s2 > qend:
                    break
                if e2 == TRACK_TYPE:
                    ttype = _read_uint(body, q2, s2)
                elif e2 == CODEC_PRIVATE:
                    priv = bytes(body[q2:q2 + s2])
                q = q2 + s2
            if ttype == 17 and priv:
                out.append(priv)
        pos = p + size
    return out


def build_cue_points(cues_buf, scale_ns, seg_off, video_track=None):
    """Walk a Cues element body -> sorted list of (time_s, absolute_byte_offset).
    Byte offsets in CueClusterPosition are relative to Segment data, so we add
    seg_off to get absolute file positions.

    ``video_track``: prefer that track's CueTrackPositions. A CuePoint can index
    several tracks, and audio/subtitle positions point at clusters far from the
    video keyframe (using them yields inverted/overlapping byte windows). The
    final monotonic filter guards against any that still slip through."""
    points = []
    pos = 0
    end = len(cues_buf)
    eid, pos = _read_id(cues_buf, pos)
    if eid != CUES:
        return []
    _size, pos = _read_size(cues_buf, pos)
    while pos < end - 1:
        try:
            e, p = _read_id(cues_buf, pos)
            size, p = _read_size(cues_buf, p)
        except (IndexError, ValueError):
            break
        if e == CUE_POINT:
            t, off = _cue_point(cues_buf, p, min(p + size, end), video_track)
            if t is not None and off is not None:
                points.append((t * scale_ns / 1e9, seg_off + off))
        pos = p + size
    points.sort()
    # keep only strictly increasing (time, offset); a backward offset would
    # produce an inverted byte range that ffmpeg can't parse
    clean = []
    for t, off in points:
        if clean and (off <= clean[-1][1] or t <= clean[-1][0]):
            continue
        clean.append((t, off))
    return clean


def _cue_point(buf, pos, end, want_track=None):
    """-> (cue_time_ticks, cluster_offset) from one CuePoint, either may be None.
    Prefers the CueTrackPositions of ``want_track``; falls back to the first one
    (the video track is conventionally listed first)."""
    t = None
    first_off = want_off = None
    while pos < end - 1:
        e, p = _read_id(buf, pos)
        size, p = _read_size(buf, p)
        if e == CUE_TIME:
            t = _read_uint(buf, p, size)
        elif e == CUE_TRACK_POS:
            trk = off = None
            q, qend = p, min(p + size, end)
            while q < qend - 1:
                e2, q2 = _read_id(buf, q)
                s2, q2 = _read_size(buf, q2)
                if e2 == CUE_CLUSTER_POSITION:
                    off = _read_uint(buf, q2, s2)
                elif e2 == CUE_TRACK:
                    trk = _read_uint(buf, q2, s2)
                q = q2 + s2
            if off is not None:
                if first_off is None:
                    first_off = off
                if want_track is not None and trk == want_track and want_off is None:
                    want_off = off
        pos = p + size
    return t, (want_off if want_off is not None else first_off)


def build_cue_times(cues_buf, scale_ns):
    """Back-compat: keyframe times only (seconds)."""
    return [t for t, _ in build_cue_points(cues_buf, scale_ns, 0)]


def fetch_range(url, start, end, headers=None):
    """Fetch bytes [start, end) (end=None => to EOF). Returns bytes."""
    return netio.fetch_range(url, start, end, UA, headers, timeout=60)


def open_range(url, start, end, headers=None):
    """Same range as fetch_range but as an open, incrementally readable response.
    Lets a consumer start work on the first bytes instead of waiting for the whole
    range to land. Caller must close it (it is a context manager)."""
    return netio.open_url(url, UA, headers, rng=(start, end), timeout=60)


def seek_plan(url, headers=None, file_size=None):
    """Main entry. Returns a dict with everything needed to serve byte-ranged
    segments, or None if there's no usable Cues index:
      {"header_size": int,          # bytes [0,header_size) = EBML+SeekHead+...
       "cues": [(time_s, abs_off)], # keyframe -> absolute cluster byte offset
       "file_size": int}
    header_size is the offset of the FIRST cluster (everything before it is the
    init/header we must prepend to every segment)."""
    try:
        header = _get_range(url, 0, HEADER_PROBE, headers)
        seg_off = parse_ebml_header(header)
        seeks = parse_seek_head(header, seg_off)
        if CUES not in seeks:
            return None
        cues_abs = seg_off + seeks[CUES]
        scale = _timestamp_scale(header, seg_off)
        length = MAX_CUES
        if file_size:
            length = min(MAX_CUES, file_size - cues_abs)
        cues_buf = _get_range(url, cues_abs, length, headers)
        vtrack = _video_track_number(header, seg_off)
        pts = build_cue_points(cues_buf, scale, seg_off, video_track=vtrack)
        if not pts:
            return None
        header_size = pts[0][1]           # first cluster offset = end of header
        return {"header_size": header_size, "cues": pts, "file_size": file_size,
                "segment_offset": seg_off}
    except Exception:
        return None


def keyframe_times(url, headers=None, file_size=None):
    """Back-compat: keyframe times only."""
    plan = seek_plan(url, headers, file_size)
    return [t for t, _ in plan["cues"]] if plan else None


def segment_grid(plan, duration, target=6.0):
    """Coalesce keyframes into >= `target`-second segments, each carrying its
    byte range so we can fetch it directly (no ffmpeg HTTP seek). Returns
    list of dicts: {"start","end","boff","bend"} where [boff,bend) are the
    cluster bytes to prepend the header to. Contiguous + non-overlapping."""
    if not plan or not plan.get("cues"):
        return None
    cues = plan["cues"]                    # [(time, abs_offset)], sorted
    fsize = plan.get("file_size")
    # pick boundary indices so each window spans >= target seconds
    idx = [0]
    last_t = cues[0][0]
    for j in range(1, len(cues)):
        if cues[j][0] - last_t >= target:
            idx.append(j)
            last_t = cues[j][0]
    wins = []
    for k, j in enumerate(idx):
        start = cues[j][0]
        boff = cues[j][1]
        if k + 1 < len(idx):
            nj = idx[k + 1]
            end = cues[nj][0]
            bend = cues[nj][1]
        else:
            end = duration
            bend = fsize if fsize else None    # to EOF
        # bend > boff guard: an inverted byte range (bad cue data) would feed
        # ffmpeg garbage; skip the window rather than serve a broken segment
        if end > start and (bend is None or bend > boff):
            wins.append({"start": start, "end": end, "boff": boff, "bend": bend})
    return wins


if __name__ == "__main__":
    import sys, json
    url = sys.argv[1]
    fs = int(sys.argv[2]) if len(sys.argv) > 2 else None
    plan = seek_plan(url, None, fs)
    if not plan:
        print(json.dumps({"cues": 0})); sys.exit(0)
    print(json.dumps({"header_size": plan["header_size"],
                      "n_cues": len(plan["cues"]),
                      "first5": [(round(t, 2), o) for t, o in plan["cues"][:5]]}))
