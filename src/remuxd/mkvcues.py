#!/usr/bin/env python3
"""
mkvcues.py — get keyframe (time, byte-offset) pairs from an MKV/WebM Cues index
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
import urllib.request

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
CUE_CLUSTER_POSITION = 0xF1
INFO          = 0x1549A966
TIMESTAMP_SCALE = 0x2AD7B1
EBML_HEADER   = 0x1A45DFA3

HEADER_PROBE = 64 * 1024
MAX_CUES     = 4 * 1024 * 1024
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120 Safari/537.36")


def _get_range(url, start, length, headers=None):
    req = urllib.request.Request(url)
    req.add_header("User-Agent", UA)
    req.add_header("Range", f"bytes={start}-{start + length - 1}")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


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
        elif eid == 0x1F43B675:                   # Cluster -> we've gone too far
            break
        if size < 0:                              # unknown size -> can't skip safely
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
        if size < 0:
            break
        pos = p + size
    return 1_000_000


def build_cue_points(cues_buf, scale_ns, seg_off):
    """Walk a Cues element body -> sorted list of (time_s, absolute_byte_offset).
    Byte offsets in CueClusterPosition are relative to Segment data, so we add
    seg_off to get absolute file positions."""
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
            t, off = _cue_point(cues_buf, p, min(p + size, end))
            if t is not None and off is not None:
                points.append((t * scale_ns / 1e9, seg_off + off))
        pos = p + size
    return sorted(points)


def _cue_point(buf, pos, end):
    """-> (cue_time_ticks, cluster_offset) from one CuePoint, either may be None."""
    t = off = None
    while pos < end - 1:
        e, p = _read_id(buf, pos)
        size, p = _read_size(buf, p)
        if e == CUE_TIME:
            t = _read_uint(buf, p, size)
        elif e == CUE_TRACK_POS:
            q, qend = p, min(p + size, end)
            while q < qend - 1:
                e2, q2 = _read_id(buf, q)
                s2, q2 = _read_size(buf, q2)
                if e2 == CUE_CLUSTER_POSITION:
                    off = _read_uint(buf, q2, s2)
                q = q2 + s2
        pos = p + size
    return t, off


def build_cue_times(cues_buf, scale_ns):
    """Back-compat: keyframe times only (seconds)."""
    return [t for t, _ in build_cue_points(cues_buf, scale_ns, 0)]


def fetch_range(url, start, end, headers=None):
    """Fetch bytes [start, end) (end=None => to EOF). Returns bytes."""
    length = (end - start) if end is not None else (MAX_CUES * 512)  # big cap for tail
    if end is None:
        # open-ended: let the server send to EOF
        req = urllib.request.Request(url)
        req.add_header("User-Agent", UA)
        req.add_header("Range", f"bytes={start}-")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.read()
    return _get_range(url, start, length, headers)


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
        pts = build_cue_points(cues_buf, scale, seg_off)
        if not pts:
            return None
        header_size = pts[0][1]           # first cluster offset = end of header
        return {"header_size": header_size, "cues": pts, "file_size": file_size}
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
        if end > start:
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
