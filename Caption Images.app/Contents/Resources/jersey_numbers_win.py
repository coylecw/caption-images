"""Jersey numbers on Windows: a free, offline stand-in for the Mac "jersey-numbers" tool.

The Mac tool uses Apple Vision to find each player (pose detection), crop their torso, and
read the number there, with a tile sweep as a fallback and a personal CoreML model on top.
There's no free, no-GPU equivalent of Vision's pose model to find players with on Windows, so
this reads numbers a different way: RapidOCR (open source, ONNX Runtime, CPU, no model
download beyond what pip installs) finds text anywhere in the photo, run once on the whole
frame and again on four overlapping quadrants at higher effective resolution (a tile sweep,
the same idea the Mac tool falls back to). Reads that land in the same spot in more than one
pass are treated as the same jersey number and vote on what it says.

What this does NOT do (out of scope for this pass, needs a Mac):
  - find players by pose/body detection, so there's no torso crop and no "Teach numbers"
    training; every read's "torso" is None and "model" is always 0.
  - "on a player" is a size/shape guess, not a real player detection.

Returns the same shape caption.py's Mac path returns, so app.html doesn't need to know which
platform produced a read: [{"n", "alts", "onPlayer", "votes", "agree", "model", "color",
"text", "torso"}, ...].
"""
import re
import threading
from collections import Counter

DIGIT_RE = re.compile(r"^\d{1,2}$")

# RapidOCR's own default confidence floor (0.5) is tuned for whole words; a 1-2 digit jersey
# number reads lower than a word simply for having less context to lean on (e.g. a correctly
# read "7" scored ~0.32 in testing) and gets silently dropped at the default. Lowering it here
# lets those through; _summarize()'s onPlayer check (below) is what keeps the resulting noise
# (stray low-confidence single-pass "reads" of background texture) from being treated as real.
TEXT_SCORE = 0.3

MAX_FULL_PX = 1800  # long edge for the quick whole-photo pass
TILE_GRID = (2, 2)  # columns, rows for the detail sweep
TILE_OVERLAP = 0.15  # fraction of a tile's size shared with its neighbours
MAX_TILE_PX = 1400  # long edge per tile, after cropping
CLUSTER_FRACTION = 0.6  # how close two reads' centers must be (relative to their size) to be "the same number"

_ENGINE = None
_ENGINE_LOCK = threading.Lock()
_UNAVAILABLE = False


def _engine():
    """The RapidOCR engine, built once and reused. None if it isn't installed."""
    global _ENGINE, _UNAVAILABLE
    if _ENGINE is not None or _UNAVAILABLE:
        return _ENGINE
    with _ENGINE_LOCK:
        if _ENGINE is not None or _UNAVAILABLE:
            return _ENGINE
        try:
            from rapidocr_onnxruntime import RapidOCR
            _ENGINE = RapidOCR()
        except Exception:
            _UNAVAILABLE = True
    return _ENGINE


def available():
    return _engine() is not None


def _passes(im):
    """[(image, x_offset, y_offset, scale), ...]: the whole photo, then an overlapping tile sweep."""
    import numpy as np  # noqa: F401  (ensures a clear ImportError up front if numpy is missing)

    w, h = im.size
    out = []
    scale = min(1.0, MAX_FULL_PX / max(w, h))
    full = im.resize((max(1, round(w * scale)), max(1, round(h * scale)))) if scale < 1 else im
    out.append((full, 0, 0, scale))

    cols, rows = TILE_GRID
    tw, th = w / cols, h / rows
    ow, oh = tw * TILE_OVERLAP, th * TILE_OVERLAP
    for row in range(rows):
        for col in range(cols):
            x0 = max(0, col * tw - ow)
            y0 = max(0, row * th - oh)
            x1 = min(w, (col + 1) * tw + ow)
            y1 = min(h, (row + 1) * th + oh)
            tile = im.crop((int(x0), int(y0), int(x1), int(y1)))
            if tile.width < 20 or tile.height < 20:
                continue
            tscale = min(1.0, MAX_TILE_PX / max(tile.size))
            if tscale < 1.0:
                tile = tile.resize((max(1, round(tile.width * tscale)), max(1, round(tile.height * tscale))))
            out.append((tile, x0, y0, tscale))
    return out


def _center(box):
    x0, y0, x1, y1 = box
    return (x0 + x1) / 2, (y0 + y1) / 2


def _size(box):
    x0, y0, x1, y1 = box
    return max(1.0, max(x1 - x0, y1 - y0))


def _cluster(raw):
    """Group reads that landed in about the same place (regardless of what they say)."""
    clusters = []
    for d in raw:
        cx, cy = _center(d["box"])
        sz = _size(d["box"])
        for cl in clusters:
            ccx, ccy = _center(cl[0]["box"])
            csz = _size(cl[0]["box"])
            near = CLUSTER_FRACTION * max(sz, csz)
            if abs(cx - ccx) < near and abs(cy - ccy) < near:
                cl.append(d)
                break
        else:
            clusters.append([d])
    return clusters


def _union_box(boxes):
    x0 = min(b[0] for b in boxes)
    y0 = min(b[1] for b in boxes)
    x1 = max(b[2] for b in boxes)
    y1 = max(b[3] for b in boxes)
    return x0, y0, x1, y1


def _sample_color(path, box):
    """Average color just around (and a bit below) a number, as a rough stand-in for jersey colour."""
    try:
        from PIL import Image
        x0, y0, x1, y1 = box
        bw, bh = x1 - x0, y1 - y0
        pad_x, pad_y = bw * 0.6, bh * 1.2
        with Image.open(path) as im:
            im = im.convert("RGB")
            w, h = im.size
            cx0, cy0 = max(0, int(x0 - pad_x)), max(0, int(y0 - pad_y))
            cx1, cy1 = min(w, int(x1 + pad_x)), min(h, int(y1 + pad_y * 2))
            if cx1 <= cx0 or cy1 <= cy0:
                return None
            region = im.crop((cx0, cy0, cx1, cy1)).resize((1, 1))
            return list(region.getpixel((0, 0)))
    except Exception:
        return None


def _summarize(cluster, img_h, path):
    counts = Counter(d["n"] for d in cluster)
    n, top_count = counts.most_common(1)[0]
    alts = [v for v, _ in counts.most_common() if v != n][:3]
    agree = top_count / len(cluster)
    conf = max(d["conf"] for d in cluster if d["n"] == n)
    box = _union_box([d["box"] for d in cluster if d["n"] == n])
    bh = box[3] - box[1]
    # a loose stand-in for "found on a player": a plausible size for a chest number (not a
    # scoreboard digit or stray background text), seen more than once across the sweep's
    # passes (two independent reads agreeing is good evidence even at low confidence each —
    # short 1-2 digit strings read lower than whole words simply for having less context), or
    # read with real confidence even just once
    on_player = 0.015 * img_h <= bh <= 0.5 * img_h and ((len(cluster) >= 2 and conf >= 0.3) or conf >= 0.55)
    return {
        "n": n, "alts": alts, "onPlayer": bool(on_player), "votes": len(cluster),
        "agree": round(agree, 2), "model": 0, "color": _sample_color(path, box),
        "text": n, "torso": None,
    }


def read_numbers(path):
    """[{"n", "alts", "onPlayer", "votes", "agree", "model", "color", "text", "torso"}, ...] for one photo."""
    engine = _engine()
    if engine is None:
        return []
    from PIL import Image
    import numpy as np

    with Image.open(path) as opened:
        im = opened.convert("RGB")
    w, h = im.size

    raw = []
    for crop, ox, oy, scale in _passes(im):
        try:
            result, _ = engine(np.array(crop), text_score=TEXT_SCORE)
        except Exception:
            continue
        if not result:
            continue
        for box, text, conf in result:
            n = re.sub(r"[^0-9]", "", text)
            if not DIGIT_RE.match(n):
                continue
            xs, ys = [p[0] for p in box], [p[1] for p in box]
            raw.append({
                "n": n, "conf": float(conf),
                "box": (min(xs) / scale + ox, min(ys) / scale + oy, max(xs) / scale + ox, max(ys) / scale + oy),
            })

    return [_summarize(c, h, path) for c in _cluster(raw)]
