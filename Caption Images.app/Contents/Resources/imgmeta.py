"""Photo metadata and previews, using only the Python standard library.

Fields this app reads and writes:
  caption   XMP dc:description, IPTC Caption-Abstract (2:120), EXIF ImageDescription
  creator   XMP dc:creator,     IPTC By-line (2:80),          EXIF Artist
  rating    XMP xmp:Rating (1-5 stars, -1 = rejected), the field Lightroom,
            Bridge and Photo Mechanic use for star ratings
  keywords  XMP dc:subject,     IPTC Keywords (2:25)  - a list
  people    XMP Iptc4xmpExt:PersonInImage ("Person Shown") - a list

JPEG and PNG files get the metadata written inside the file.
RAW, HEIC and TIFF files get a Lightroom-style sidecar (IMG_0001.xmp next to
IMG_0001.CR3); the original file is never modified.

Previews: RAW files carry JPEG previews made by the camera. They are read
directly (milliseconds) instead of rendering the RAW data.
"""
import hashlib
import html
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import zlib

EMBED_EXTS = {".jpg", ".jpeg", ".png"}
SUPPORTED = EMBED_EXTS  # kept for older callers
RAW_EXTS = {".cr2", ".cr3", ".crw", ".nef", ".nrw", ".arw", ".srf", ".sr2", ".dng", ".raf", ".orf",
            ".rw2", ".pef", ".srw", ".3fr", ".iiq", ".x3f", ".erf", ".mos", ".kdc", ".mrw"}
SIDECAR_EXTS = RAW_EXTS | {".heic", ".heif", ".tif", ".tiff"}
PHOTO_EXTS = EMBED_EXTS | SIDECAR_EXTS

FIELDS = {
    "caption": {"xmp": "dc:description", "iptc": 120, "exif": 0x010E, "png": "Description"},
    "creator": {"xmp": "dc:creator", "iptc": 80, "exif": 0x013B, "png": "Author"},
    "rating": {"xmp": "xmp:Rating"},
    "keywords": {"xmp": "dc:subject", "iptc": 25, "list": True},
    "people": {"xmp": "Iptc4xmpExt:PersonInImage", "list": True},
    # Photo Mechanic's tag (the checkbox it shows on each thumbnail; T toggles it)
    "tagged": {"xmp": "photomechanic:Tagged"},
    # Photo Mechanic's color class (0 none, 1 Winner ... 8 Trash) and its packed
    # "tagged:colorclass:rating:frame" field, kept in step so Photo Mechanic agrees with us
    "colorclass": {"xmp": "photomechanic:ColorClass"},
    "pmprefs": {"xmp": "photomechanic:Prefs"},
    # Metadata template fields (IPTC Core), written at ingest
    "title": {"xmp": "dc:title", "iptc": 5},
    "headline": {"xmp": "photoshop:Headline", "iptc": 105},
    "copyright": {"xmp": "dc:rights", "iptc": 116, "exif": 0x8298, "png": "Copyright"},
    "credit": {"xmp": "photoshop:Credit", "iptc": 110},
    "source": {"xmp": "photoshop:Source", "iptc": 115},
    "location": {"xmp": "Iptc4xmpCore:Location", "iptc": 92},
    "city": {"xmp": "photoshop:City", "iptc": 90},
    "state": {"xmp": "photoshop:State", "iptc": 95},
    "country": {"xmp": "photoshop:Country", "iptc": 101},
    "jobid": {"xmp": "photoshop:TransmissionReference", "iptc": 103},
    "instructions": {"xmp": "photoshop:Instructions", "iptc": 40},
    "bylinetitle": {"xmp": "photoshop:AuthorsPosition", "iptc": 85},  # Getty: Staff / Contributor / Stringer
    "countrycode": {"xmp": "Iptc4xmpCore:CountryCode", "iptc": 100},  # Getty: USA
}
TEMPLATE_FIELDS = ("title", "headline", "copyright", "credit", "source", "location", "city", "state", "country",
                   "jobid", "instructions", "bylinetitle", "countrycode")
LIST_PROPS = {f["xmp"] for f in FIELDS.values() if f.get("list")}


def _blank(v):
    return v is None or v == "" or v == []


def ext_of(path):
    return os.path.splitext(path)[1].lower()


# ---------------------------------------------------------------- XMP ----

XMP_HEADER = b"http://ns.adobe.com/xap/1.0/\x00"
NAMESPACES = {"dc": "http://purl.org/dc/elements/1.1/", "xmp": "http://ns.adobe.com/xap/1.0/",
              "Iptc4xmpExt": "http://iptc.org/std/Iptc4xmpExt/2008-02-29/",
              "photoshop": "http://ns.adobe.com/photoshop/1.0/",
              "Iptc4xmpCore": "http://iptc.org/std/Iptc4xmpCore/1.0/xmlns/",
              "photomechanic": "http://ns.camerabits.com/photomechanic/1.0/"}
LI_RE = re.compile(r"<rdf:li\b[^>]*>(.*?)</rdf:li>", re.S)
EMPTY_BLOCK_RE = re.compile(r'<rdf:Description rdf:about=""(?:\s+xmlns:\w+="[^"]*")*\s*>\s*</rdf:Description>')
NEW_XMP = (
    '<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>\n'
    '<x:xmpmeta xmlns:x="adobe:ns:meta/">\n'
    ' <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">\n'
    "{}</rdf:RDF>\n</x:xmpmeta>\n"
    '<?xpacket end="w"?>'
)


def _el_re(q):
    return re.compile(r"<%s\b[^>]*?(?:/>|>.*?</%s>)" % (q, q), re.S)


def _attr_re(q):
    return re.compile(r'\s%s\s*=\s*"([^"]*)"' % q)


def _xmp_el(q, text):
    if q in LIST_PROPS:
        lis = "".join("<rdf:li>%s</rdf:li>" % html.escape(str(x), quote=True) for x in text)
        return "<{0}><rdf:Bag>{1}</rdf:Bag></{0}>".format(q, lis)
    t = html.escape(str(text), quote=True)
    if q == "dc:creator":
        return "<dc:creator><rdf:Seq><rdf:li>%s</rdf:li></rdf:Seq></dc:creator>" % t
    if q in ("dc:description", "dc:rights", "dc:title"):
        return '<{0}><rdf:Alt><rdf:li xml:lang="x-default">{1}</rdf:li></rdf:Alt></{0}>'.format(q, t)
    return "<{0}>{1}</{0}>".format(q, t)


def xmp_get(xmp, q):
    if not xmp:
        return None
    m = _el_re(q).search(xmp)
    if m:
        s = m.group(0)
        lis = [html.unescape(x) for x in LI_RE.findall(s)]
        if q in LIST_PROPS:
            return [x.strip() for x in lis if x.strip()]
        if q == "dc:creator":
            return "; ".join(lis)
        if q.startswith("dc:"):
            return lis[0] if lis else ""
        if s.endswith("/>"):
            return ""
        return html.unescape(re.sub(r"^<[^>]*>|</[^>]*>$", "", s).strip())
    m = _attr_re(q).search(xmp)
    if m and q in LIST_PROPS:
        return [x.strip() for x in html.unescape(m.group(1)).split(",") if x.strip()]
    return html.unescape(m.group(1)) if m else None


def xmp_set(xmp, values):
    """values: {qualified name: text or list}. '' or [] removes the property."""
    body = "".join("   " + _xmp_el(q, t) + "\n" for q, t in values.items() if not _blank(t))
    if body:
        prefixes = sorted({q.split(":")[0] for q, t in values.items() if not _blank(t)})
        ns = "".join('\n    xmlns:%s="%s"' % (p, NAMESPACES[p]) for p in prefixes)
        block = '  <rdf:Description rdf:about=""%s>\n%s  </rdf:Description>\n' % (ns, body)
    else:
        block = ""
    if not xmp:
        return NEW_XMP.format(block)
    for q in values:
        xmp = _attr_re(q).sub("", xmp)
        xmp = _el_re(q).sub("", xmp)
    xmp = EMPTY_BLOCK_RE.sub("", xmp)
    if not block:
        return xmp
    if "</rdf:RDF>" not in xmp:
        return NEW_XMP.format(block)
    return xmp.replace("</rdf:RDF>", block + " </rdf:RDF>", 1)


# --------------------------------------------------------------- JPEG ----

def _jpeg_split(data):
    if data[:2] != b"\xff\xd8":
        raise ValueError("Not a JPEG file")
    i, segs = 2, []
    while i < len(data) - 1:
        if data[i] != 0xFF:
            raise ValueError("JPEG structure is damaged")
        marker = data[i + 1]
        if marker == 0xFF:
            i += 1
            continue
        if marker == 0xDA:  # start of scan: the rest is image data
            return segs, data[i:]
        if 0xD0 <= marker <= 0xD7 or marker == 0x01:
            segs.append([marker, b""])
            i += 2
            continue
        (length,) = struct.unpack(">H", data[i + 2:i + 4])
        segs.append([marker, data[i + 4:i + 2 + length]])
        i += 2 + length
    raise ValueError("JPEG has no image data")


def _jpeg_header(f):
    """Read only the metadata segments of a JPEG file (stops at the image data)."""
    if f.read(2) != b"\xff\xd8":
        raise ValueError("Not a JPEG file")
    segs = []
    while True:
        b = f.read(1)
        if not b:
            raise ValueError("JPEG has no image data")
        if b != b"\xff":
            raise ValueError("JPEG structure is damaged")
        m = f.read(1)
        while m == b"\xff":
            m = f.read(1)
        if not m:
            raise ValueError("JPEG has no image data")
        marker = m[0]
        if marker == 0xDA:
            return segs
        if 0xD0 <= marker <= 0xD7 or marker == 0x01:
            segs.append([marker, b""])
            continue
        (length,) = struct.unpack(">H", f.read(2))
        segs.append([marker, f.read(length - 2)])


def _jpeg_join(segs, rest):
    out = [b"\xff\xd8"]
    for marker, payload in segs:
        if 0xD0 <= marker <= 0xD7 or marker == 0x01:
            out.append(bytes([0xFF, marker]))
            continue
        if len(payload) > 65533:
            raise ValueError("Text is too long to fit in a JPEG metadata block")
        out.append(bytes([0xFF, marker]) + struct.pack(">H", len(payload) + 2) + payload)
    out.append(rest)
    return b"".join(out)


def _find(segs, marker, prefix):
    for idx, (m, p) in enumerate(segs):
        if m == marker and p.startswith(prefix):
            return idx
    return None


# TIFF / EXIF -------------------------------------------------------------

TYPE_SIZES = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8, 13: 4}


def _tiff_endian(tiff):
    if tiff[:2] == b"II":
        return "<"
    if tiff[:2] == b"MM":
        return ">"
    raise ValueError("Not TIFF data")


def _ifd(tiff, off, e):
    """Return ({tag: (type, count, value_bytes)}, next_ifd_offset)."""
    (n,) = struct.unpack(e + "H", tiff[off:off + 2])
    if n > 1000:
        raise ValueError("Unreasonable IFD")
    out = {}
    for k in range(n):
        p = off + 2 + 12 * k
        tag, typ, count = struct.unpack(e + "HHI", tiff[p:p + 8])
        size = TYPE_SIZES.get(typ, 1) * count
        if size <= 4:
            val = tiff[p + 8:p + 8 + size]
        else:
            (vo,) = struct.unpack(e + "I", tiff[p + 8:p + 12])
            val = tiff[vo:vo + size]
        out[tag] = (typ, count, val)
    nxt = tiff[off + 2 + 12 * n:off + 6 + 12 * n]
    return out, (struct.unpack(e + "I", nxt)[0] if len(nxt) == 4 else 0)


def _ints(entry, e):
    typ, count, val = entry
    fmt = {3: "H", 4: "I", 8: "h", 9: "i", 13: "I"}.get(typ)
    if not fmt or len(val) < count * struct.calcsize(fmt):
        return []
    return list(struct.unpack(e + fmt * count, val[:count * struct.calcsize(fmt)]))


def _text(entry):
    return entry[2].rstrip(b"\x00").decode("utf-8", "replace")


def _exif_get(tiff, want):
    try:
        e = _tiff_endian(tiff)
        tags, _ = _ifd(tiff, struct.unpack(e + "I", tiff[4:8])[0], e)
        return _text(tags[want]) if want in tags else None
    except (struct.error, IndexError, ValueError, KeyError):
        return None


def _tiff_info(tiff):
    """Orientation and capture time from a TIFF/EXIF block."""
    info = {}
    try:
        e = _tiff_endian(tiff)
        tags, _ = _ifd(tiff, struct.unpack(e + "I", tiff[4:8])[0], e)
        if 0x0112 in tags:
            v = _ints(tags[0x0112], e)
            if v:
                info["orientation"] = v[0]
        if 0x8769 in tags:
            exif, _ = _ifd(tiff, _ints(tags[0x8769], e)[0], e)
            for t in (0x9003, 0x9004):
                if t in exif:
                    info["time"] = _text(exif[t])
                    break
        if "time" not in info and 0x0132 in tags:
            info["time"] = _text(tags[0x0132])
    except (struct.error, IndexError, ValueError, KeyError):
        pass
    return info


def _exif_set(tiff, values):
    """values: {tag: text}. Rewrites IFD0 at the end of the TIFF block so no existing offset moves."""
    if tiff is None:
        if not any(values.values()):
            return None
        tiff = b"MM\x00\x2a" + struct.pack(">I", 8) + struct.pack(">HI", 0, 0)
    e = _tiff_endian(tiff)
    (ifd,) = struct.unpack(e + "I", tiff[4:8])
    (n,) = struct.unpack(e + "H", tiff[ifd:ifd + 2])
    entries = [tiff[ifd + 2 + 12 * k:ifd + 14 + 12 * k] for k in range(n)]
    next_ifd = tiff[ifd + 2 + 12 * n:ifd + 6 + 12 * n]
    tag_of = lambda ent: struct.unpack(e + "H", ent[:2])[0]  # noqa: E731
    present = {tag_of(ent) for ent in entries}
    if not any(values.values()) and not (present & set(values)):
        return tiff
    entries = [ent for ent in entries if tag_of(ent) not in values]

    body = bytearray(tiff)
    if len(body) % 2:
        body += b"\x00"
    new_ifd = len(body)
    strings = [(tag, t.encode("utf-8") + b"\x00") for tag, t in values.items() if t]
    count = len(entries) + len(strings)
    data_off = new_ifd + 2 + 12 * count + 4
    extra = b""
    for tag, raw in strings:
        if len(raw) <= 4:
            value = raw.ljust(4, b"\x00")
        else:
            value = struct.pack(e + "I", data_off + len(extra))
            extra += raw + (b"\x00" if len(raw) % 2 else b"")
        entries.append(struct.pack(e + "HHI", tag, 2, len(raw)) + value)
    entries.sort(key=tag_of)
    body += struct.pack(e + "H", count) + b"".join(entries) + next_ifd + extra
    body[4:8] = struct.pack(e + "I", new_ifd)
    return bytes(body)


# IPTC ------------------------------------------------------------------

PS_HEADER = b"Photoshop 3.0\x00"
UTF8_MARK = b"\x1b%G"


def _irb_parse(data):
    blocks, i = [], 0
    while i + 12 <= len(data) and data[i:i + 4] == b"8BIM":
        (rid,) = struct.unpack(">H", data[i + 4:i + 6])
        name_len = 1 + data[i + 6]
        name_len += name_len % 2
        name = data[i + 6:i + 6 + name_len]
        j = i + 6 + name_len
        (size,) = struct.unpack(">I", data[j:j + 4])
        blocks.append([rid, name, data[j + 4:j + 4 + size]])
        i = j + 4 + size + size % 2
    return blocks


def _irb_build(blocks):
    out = []
    for rid, name, body in blocks:
        out.append(b"8BIM" + struct.pack(">H", rid) + name + struct.pack(">I", len(body)) + body)
        if len(body) % 2:
            out.append(b"\x00")
    return b"".join(out)


def _iptc_parse(data):
    out, i = [], 0
    while i + 5 <= len(data) and data[i] == 0x1C:
        rec, ds = data[i + 1], data[i + 2]
        (ln,) = struct.unpack(">H", data[i + 3:i + 5])
        start = i + 5
        if ln & 0x8000:
            nbytes = ln & 0x7FFF
            ln = int.from_bytes(data[start:start + nbytes], "big")
            start += nbytes
        out.append((rec, ds, data[start:start + ln]))
        i = start + ln
    return out


def _iptc_build(items):
    out = []
    for rec, ds, val in items:
        if len(val) < 0x8000:
            out.append(struct.pack(">BBBH", 0x1C, rec, ds, len(val)) + val)
        else:
            out.append(struct.pack(">BBBH", 0x1C, rec, ds, 0x8004) + struct.pack(">I", len(val)) + val)
    return b"".join(out)


def _decode(raw, utf8):
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("utf-8" if utf8 else "latin-1", "replace")


def _iptc_get(app13, want, as_list=False):
    for rid, _, body in _irb_parse(app13[len(PS_HEADER):]):
        if rid == 0x0404:
            items = _iptc_parse(body)
            utf8 = any(r == 1 and d == 90 and v == UTF8_MARK for r, d, v in items)
            vals = [_decode(v, utf8) for r, d, v in items if r == 2 and d == want]
            if vals:
                return [v.strip() for v in vals if v.strip()] if as_list else "; ".join(vals)
    return None


def _iptc_set(app13, values):
    """values: {dataset number in record 2: text}."""
    blocks = _irb_parse(app13[len(PS_HEADER):]) if app13 else []
    iptc = next((b for b in blocks if b[0] == 0x0404), None)
    items = _iptc_parse(iptc[2]) if iptc else []
    if not any(values.values()) and not any(r == 2 and d in values for r, d, _ in items):
        return app13
    items = [it for it in items if not (it[0] == 2 and it[1] in values)]
    if any(values.values()):
        items = [it for it in items if not (it[0] == 1 and it[1] == 90)]
        items.append((1, 90, UTF8_MARK))
        if not any(r == 2 and d == 0 for r, d, _ in items):
            items.append((2, 0, b"\x00\x04"))
        for ds, t in values.items():
            for one in (t if isinstance(t, list) else [t]):
                if one:
                    items.append((2, ds, one.encode("utf-8")[:64] if ds == 25 else one.encode("utf-8")))
    items.sort(key=lambda it: (it[0], it[1]))  # stable: keeps repeated keywords in order
    data = _iptc_build(items)
    if iptc:
        iptc[2] = data
    else:
        blocks.append([0x0404, b"\x00\x00", data])
    for b in blocks:  # Photoshop's checksum of the IPTC data
        if b[0] == 0x0425:
            b[2] = hashlib.md5(data).digest()
    return PS_HEADER + _irb_build(blocks)


# JPEG metadata -----------------------------------------------------------

def _jpeg_get(segs):
    i = _find(segs, 0xE1, XMP_HEADER)
    xmp = segs[i][1][len(XMP_HEADER):].decode("utf-8", "replace") if i is not None else None
    i = _find(segs, 0xED, PS_HEADER)
    app13 = segs[i][1] if i is not None else None
    i = _find(segs, 0xE1, b"Exif\x00\x00")
    tiff = segs[i][1][6:] if i is not None else None
    out = {}
    for key, f in FIELDS.items():
        v = xmp_get(xmp, f["xmp"])
        if v is None and app13 and "iptc" in f:
            v = _iptc_get(app13, f["iptc"], f.get("list", False))
        if v is None and tiff and "exif" in f:
            v = _exif_get(tiff, f["exif"])
        out[key] = v or ([] if f.get("list") else "")
    if out.get("rating") == "" and tiff:  # not rated since: the camera's EXIF rating
        r = _exif_rating(tiff)
        if r:
            out["rating"] = str(r)
    return out


def _insert_pos(segs, after_app0_only=False):
    if after_app0_only:
        return 1 if segs and segs[0][0] == 0xE0 else 0
    pos = 0
    while pos < len(segs) and 0xE0 <= segs[pos][0] <= 0xEF:
        pos += 1
    return pos


def _jpeg_set(data, values):
    segs, rest = _jpeg_split(data)
    exif_vals = {FIELDS[k]["exif"]: v for k, v in values.items() if "exif" in FIELDS[k]}
    iptc_vals = {FIELDS[k]["iptc"]: v for k, v in values.items() if "iptc" in FIELDS[k]}
    xmp_vals = {FIELDS[k]["xmp"]: v for k, v in values.items()}

    if exif_vals:
        i = _find(segs, 0xE1, b"Exif\x00\x00")
        tiff = _exif_set(segs[i][1][6:] if i is not None else None, exif_vals)
        if i is not None:
            segs[i][1] = b"Exif\x00\x00" + tiff
        elif tiff is not None:
            segs.insert(_insert_pos(segs, True), [0xE1, b"Exif\x00\x00" + tiff])

    i = _find(segs, 0xE1, XMP_HEADER)
    old = segs[i][1][len(XMP_HEADER):].decode("utf-8", "replace") if i is not None else None
    if old is not None or any(not _blank(v) for v in xmp_vals.values()):
        payload = XMP_HEADER + xmp_set(old, xmp_vals).encode("utf-8")
        if i is not None:
            segs[i][1] = payload
        else:
            segs.insert(_insert_pos(segs), [0xE1, payload])

    if iptc_vals:
        i = _find(segs, 0xED, PS_HEADER)
        app13 = _iptc_set(segs[i][1] if i is not None else None, iptc_vals)
        if i is not None:
            segs[i][1] = app13
        elif app13:
            segs.insert(_insert_pos(segs), [0xED, app13])

    return _jpeg_join(segs, rest)


# ---------------------------------------------------------------- PNG ----

PNG_SIG = b"\x89PNG\r\n\x1a\n"
TEXT_CHUNKS = (b"tEXt", b"iTXt", b"zTXt")


def _png_chunks(data):
    if data[:8] != PNG_SIG:
        raise ValueError("Not a PNG file")
    i, chunks = 8, []
    while i < len(data):
        (ln,) = struct.unpack(">I", data[i:i + 4])
        typ = data[i + 4:i + 8]
        chunks.append((typ, data[i + 8:i + 8 + ln]))
        i += 12 + ln
        if typ == b"IEND":
            break
    return chunks


def _png_text_chunks(f):
    """Read only the text chunks of a PNG file, skipping image data."""
    if f.read(8) != PNG_SIG:
        raise ValueError("Not a PNG file")
    out = []
    while True:
        hdr = f.read(8)
        if len(hdr) < 8:
            break
        (ln,), typ = struct.unpack(">I", hdr[:4]), hdr[4:]
        if typ in TEXT_CHUNKS:
            out.append((typ, f.read(ln)))
            f.seek(4, 1)
        else:
            f.seek(ln + 4, 1)
        if typ == b"IEND":
            break
    return out


def _png_chunk(typ, body):
    return struct.pack(">I", len(body)) + typ + body + struct.pack(">I", zlib.crc32(typ + body) & 0xFFFFFFFF)


def _png_text(typ, body):
    """Return (keyword, text) for tEXt/iTXt/zTXt chunks."""
    key, _, rest = body.partition(b"\x00")
    key = key.decode("latin-1")
    if typ == b"tEXt":
        return key, rest.decode("latin-1")
    if typ == b"zTXt":
        return key, zlib.decompress(rest[1:]).decode("latin-1")
    comp = rest[0]
    _lang, _, rest = rest[2:].partition(b"\x00")
    _trans, _, txt = rest.partition(b"\x00")
    if comp:
        txt = zlib.decompress(txt)
    return key, txt.decode("utf-8", "replace")


def _itxt(key, text):
    return key.encode("latin-1") + b"\x00\x00\x00\x00\x00" + text.encode("utf-8")


def _png_get(chunks):
    xmp, texts = None, {}
    for typ, body in chunks:
        if typ in TEXT_CHUNKS:
            key, txt = _png_text(typ, body)
            if key == "XML:com.adobe.xmp":
                xmp = txt
            else:
                texts[key] = txt
    out = {}
    for key, f in FIELDS.items():
        v = xmp_get(xmp, f["xmp"])
        out[key] = v if v is not None else ([] if f.get("list") else texts.get(f.get("png"), ""))
    return out


def _png_set(data, values):
    keywords = {FIELDS[k]["png"] for k in values if "png" in FIELDS[k]}
    xmp, kept = None, []
    for typ, body in _png_chunks(data):
        if typ in TEXT_CHUNKS:
            key, txt = _png_text(typ, body)
            if key == "XML:com.adobe.xmp":
                xmp = txt
                continue
            if key in keywords:
                continue
        kept.append((typ, body))
    new = []
    xmp_vals = {FIELDS[k]["xmp"]: v for k, v in values.items()}
    if xmp is not None or any(not _blank(v) for v in xmp_vals.values()):
        new.append((b"iTXt", _itxt("XML:com.adobe.xmp", xmp_set(xmp, xmp_vals))))
    new.extend((b"iTXt", _itxt(FIELDS[k]["png"], v)) for k, v in values.items() if v and "png" in FIELDS[k])
    out = [PNG_SIG]
    for typ, body in kept:
        out.append(_png_chunk(typ, body))
        if typ == b"IHDR":
            out.extend(_png_chunk(t, b) for t, b in new)
    return b"".join(out)


# ------------------------------------------------------------ sidecars ----

def sidecar_path(path):
    return os.path.splitext(path)[0] + ".xmp"


def _read_text_file(path):
    with open(path, "rb") as f:
        return f.read().decode("utf-8-sig", "replace")


def _atomic_write(path, data, like=None):
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)), prefix=".caption-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        if like and os.path.exists(like):
            shutil.copymode(like, tmp)
        else:  # new file: normal permissions (mkstemp makes it owner-only)
            umask = os.umask(0)
            os.umask(umask)
            os.chmod(tmp, 0o666 & ~umask)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _sidecar_get(path):
    sc = sidecar_path(path)
    xmp = _read_text_file(sc) if os.path.isfile(sc) else None
    out = {k: xmp_get(xmp, f["xmp"]) or ([] if f.get("list") else "") for k, f in FIELDS.items()}
    if out["rating"] == "":  # not rated here, in Lightroom or in Photo Mechanic: use the camera's stars
        r = camera_rating(path)
        if r:
            out["rating"] = str(r)
    return out


XMP_UUID = bytes.fromhex("be7acfcb97a942e89c71999491e3afac")  # the XMP box inside a Canon CR3
RATING_RE = re.compile(r'xmp:Rating\s*(?:=\s*"|>)\s*(-?\d+)')


def _rating_int(v):
    try:
        r = int(v)
    except (TypeError, ValueError):
        return 0
    if r == 65535:  # -1 stored as an unsigned number
        return -1
    return r if -1 <= r <= 5 else 0


def _exif_rating(tiff):
    """EXIF Rating (tag 0x4746) from a TIFF/EXIF block, or 0."""
    try:
        e = _tiff_endian(tiff)
        ifd0, _ = _ifd(tiff, struct.unpack(e + "I", tiff[4:8])[0], e)
        if 0x4746 in ifd0:
            v = _ints(ifd0[0x4746], e)
            return _rating_int(v[0]) if v else 0
    except (ValueError, struct.error, IndexError, KeyError):
        pass
    return 0


def camera_rating(path):
    """The star rating stored in the photo file by the camera (e.g. the rating button on a Canon), or 0.
    Ratings made later live in the sidecar (RAW) or the XMP packet (JPEG) and take precedence."""
    ext = ext_of(path)
    head = b""
    try:
        with open(path, "rb") as f:
            if ext == ".cr3":
                end = os.fstat(f.fileno()).st_size
                for typ, a, b in _bmff_boxes(f, 0, end):
                    if typ == b"mdat":
                        break
                    if typ == b"uuid" and 16 < b - a < 4 * 1024 * 1024:
                        f.seek(a)
                        if f.read(16) == XMP_UUID:
                            m = RATING_RE.search(f.read(b - a - 16).decode("utf-8", "replace"))
                            return _rating_int(m.group(1)) if m else 0
                return 0
            if ext in (".jpg", ".jpeg"):
                segs = _jpeg_header(f)
                i = _find(segs, 0xE1, b"Exif\x00\x00")
                return _exif_rating(segs[i][1][6:]) if i is not None else 0
            if ext in RAW_EXTS or ext in SIDECAR_EXTS:
                head = f.read(512 * 1024)
    except (OSError, ValueError, struct.error):
        return 0
    if head[:2] in (b"II", b"MM"):
        r = _exif_rating(head)
        if r:
            return r
    m = RATING_RE.search(head.decode("latin-1"))
    return _rating_int(m.group(1)) if m else 0


def _sidecar_set(path, values):
    sc = sidecar_path(path)
    old = _read_text_file(sc) if os.path.isfile(sc) else None
    if old is None and all(_blank(v) for v in values.values()):
        return
    new = xmp_set(old, {FIELDS[k]["xmp"]: v for k, v in values.items()})
    _atomic_write(sc, new.encode("utf-8"), like=sc)


# ------------------------------------------------------------- public ----

def _clean(meta):
    try:
        r = int(float(meta.get("rating") or 0))
    except ValueError:
        r = 0
    return {"caption": meta.get("caption", ""), "creator": meta.get("creator", ""), "rating": max(-1, min(5, r)),
            "keywords": list(meta.get("keywords") or []), "people": list(meta.get("people") or []),
            "tagged": _truthy(meta.get("tagged")), "colorclass": _color(meta.get("colorclass"))}


def _truthy(v):
    return str(v or "").strip().lower() in ("true", "1", "yes")


def _color(v):
    try:
        c = int(float(v or 0))
    except ValueError:
        return 0
    return c if 0 <= c <= 8 else 0


def _raw_meta(path):
    """Every field this app knows, as stored (before cleaning up)."""
    ext = ext_of(path)
    if ext in SIDECAR_EXTS:
        return _sidecar_get(path)
    with open(path, "rb") as f:
        if ext == ".png":
            return _png_get(_png_text_chunks(f))
        if ext in (".jpg", ".jpeg"):
            return _jpeg_get(_jpeg_header(f))
    raise ValueError("Unsupported file type")


def _tag_list(items):
    out, seen = [], set()
    for x in items or []:
        x = " ".join(str(x).split())
        if x and x.lower() not in seen:
            seen.add(x.lower())
            out.append(x)
    return out


def read_meta(path):
    """Return {"caption", "creator", "rating" (-1 rejected, 0 none, 1-5), "keywords": [...], "people": [...]}."""
    ext = ext_of(path)
    if ext in SIDECAR_EXTS:
        return _clean(_sidecar_get(path))
    with open(path, "rb") as f:
        if ext == ".png":
            return _clean(_png_get(_png_text_chunks(f)))
        if ext in (".jpg", ".jpeg"):
            return _clean(_jpeg_get(_jpeg_header(f)))
    raise ValueError("Unsupported file type")


def read_template_fields(path):
    """The metadata-template fields (copyright, credit, city, ...) as {name: text}."""
    raw = _raw_meta(path)
    return {k: raw.get(k) or "" for k in TEMPLATE_FIELDS}


def write_meta(path, caption=None, creator=None, rating=None, keywords=None, people=None, extra=None, tagged=None,
               colorclass=None):
    """Set any of caption / photographer / rating / keywords / people. None leaves a field as it is.
    extra: {template field: text} for the fields in TEMPLATE_FIELDS."""
    values = {}
    for k, v in (extra or {}).items():
        if k in TEMPLATE_FIELDS and v is not None:
            values[k] = " ".join(str(v).split()) if k != "instructions" else str(v).strip()
    if keywords is not None:
        values["keywords"] = _tag_list(keywords)
    if people is not None:
        values["people"] = _tag_list(people)
    if caption is not None:
        values["caption"] = caption.replace("\r\n", "\n").strip()
    if creator is not None:
        values["creator"] = " ".join(creator.split())
    if tagged is not None:
        values["tagged"] = "True" if tagged else ""
    if colorclass is not None:
        c = _color(colorclass)
        values["colorclass"] = str(c) if c else ""
    if rating is not None:
        r = max(-1, min(5, int(rating)))
        values["rating"] = str(r) if r else ("0" if camera_rating(path) else "")
    if tagged is not None or colorclass is not None or rating is not None:
        try:
            cur = _raw_meta(path)
        except (OSError, ValueError):
            cur = {}
        prefs = str(cur.get("pmprefs") or "")
        t = bool(tagged) if tagged is not None else _truthy(cur.get("tagged"))
        c = _color(colorclass) if colorclass is not None else _color(cur.get("colorclass"))
        try:
            r = int(rating) if rating is not None else int(float(cur.get("rating") or 0))
        except ValueError:
            r = 0
        if prefs or t or c:  # Photo Mechanic may read this packed copy instead of the fields above
            parts = prefs.split(":")
            frame = parts[3] if len(parts) >= 4 and parts[3].strip() else "-00001"
            values["pmprefs"] = "%d:%d:%d:%s" % (1 if t else 0, c, max(0, min(5, r)), frame)
    if values:
        ext = ext_of(path)
        if ext in SIDECAR_EXTS:
            _sidecar_set(path, values)
        elif ext in EMBED_EXTS:
            with open(path, "rb") as f:
                data = f.read()
            new = _png_set(data, values) if ext == ".png" else _jpeg_set(data, values)
            _atomic_write(path, new, like=path)
        else:
            raise ValueError("Unsupported file type")
    return read_meta(path)


def read_caption(path):
    return read_meta(path)["caption"]


def write_caption(path, text):
    return write_meta(path, caption=text)["caption"]


# ---------------------------------------------------- capture info -------

def _bmff_boxes(f, start, end):
    pos = start
    while pos + 8 <= end:
        f.seek(pos)
        hdr = f.read(8)
        if len(hdr) < 8:
            return
        size, typ = struct.unpack(">I4s", hdr)
        h = 8
        if size == 1:
            (size,) = struct.unpack(">Q", f.read(8))
            h = 16
        elif size == 0:
            size = end - pos
        if size < h:
            return
        yield typ, pos + h, pos + size
        pos += size


CANON_UUID = bytes.fromhex("85c0b687820f11e08111f4ce462b6a48")


def _cr3_moov(f):
    f.seek(0, 2)
    end = f.tell()
    for typ, a, b in _bmff_boxes(f, 0, end):
        if typ == b"moov":
            return a, b, end
    raise ValueError("Not a CR3 file")


def _cr3_info(f):
    a, b, _ = _cr3_moov(f)
    info = {}
    for typ, ua, ub in _bmff_boxes(f, a, b):
        if typ == b"uuid":
            f.seek(ua)
            if f.read(16) != CANON_UUID:
                continue
            for t, ca, cb in _bmff_boxes(f, ua + 16, ub):
                if t in (b"CMT1", b"CMT2"):
                    f.seek(ca)
                    tiff = f.read(cb - ca)
                    try:
                        e = _tiff_endian(tiff)
                        tags, _ = _ifd(tiff, struct.unpack(e + "I", tiff[4:8])[0], e)
                    except (ValueError, struct.error):
                        continue
                    if 0x0112 in tags and _ints(tags[0x0112], e):
                        info["orientation"] = _ints(tags[0x0112], e)[0]
                    for tg in (0x9003, 0x9004, 0x0132):
                        if tg in tags and "time" not in info:
                            info["time"] = _text(tags[tg])
    return info


def capture_info(path):
    """{"time": "YYYY:MM:DD HH:MM:SS" or None, "orientation": 1-8}. Cheap: reads only headers."""
    ext = ext_of(path)
    info = {}
    try:
        with open(path, "rb") as f:
            if ext in (".jpg", ".jpeg"):
                segs = _jpeg_header(f)
                i = _find(segs, 0xE1, b"Exif\x00\x00")
                if i is not None:
                    info = _tiff_info(segs[i][1][6:])
            elif ext == ".cr3":
                info = _cr3_info(f)
            elif ext in RAW_EXTS or ext in (".tif", ".tiff"):
                head = f.read(1024 * 1024)
                if head[:2] in (b"II", b"MM"):
                    info = _tiff_info(head)
    except (OSError, ValueError, struct.error):
        pass
    return {"time": info.get("time"), "orientation": info.get("orientation", 1)}


# ------------------------------------------------------------ previews ----

def _orient_jpeg(jpeg, orientation):
    """Give a preview JPEG a minimal EXIF block so browsers rotate it correctly."""
    try:
        segs, rest = _jpeg_split(jpeg)
    except ValueError:
        return jpeg
    segs = [s for s in segs if not (s[0] == 0xE1 and s[1].startswith(b"Exif\x00\x00"))]
    if orientation and orientation != 1:
        tiff = b"MM\x00\x2a" + struct.pack(">IH", 8, 1) + struct.pack(">HHIHH", 0x0112, 3, 1, orientation, 0) + b"\x00" * 4
        segs.insert(_insert_pos(segs, True), [0xE1, b"Exif\x00\x00" + tiff])
    return _jpeg_join(segs, rest)


def _is_viewable_jpeg(f, off, length):
    """True for a baseline/progressive JPEG (not the lossless JPEG some RAWs store image data in)."""
    if length < 1024:
        return False
    f.seek(off)
    head = f.read(min(length, 65536))
    if head[:2] != b"\xff\xd8":
        return False
    i = 2
    while i + 4 <= len(head):
        if head[i] != 0xFF:
            return False
        m = head[i + 1]
        if m in (0xC0, 0xC1, 0xC2):
            return True
        if m in (0xC3, 0xC5, 0xC6, 0xC7, 0xCB, 0xCD, 0xCE, 0xCF, 0xDA):
            return False
        if m == 0xFF:
            i += 1
            continue
        (ln,) = struct.unpack(">H", head[i + 2:i + 4])
        i += 2 + ln
    return False


def _tiff_previews(f):
    """(offset, length) of every viewable JPEG embedded in a TIFF-based RAW."""
    f.seek(0)
    head = f.read(8)
    if head[:2] not in (b"II", b"MM"):
        return []
    e = "<" if head[:2] == b"II" else ">"
    f.seek(0, 2)
    size = f.tell()
    f.seek(0)
    data = f.read(min(size, 4 * 1024 * 1024))  # IFDs live near the start
    cands, seen = [], set()
    todo = [struct.unpack(e + "I", data[4:8])[0]]
    while todo and len(seen) < 40:
        off = todo.pop(0)
        if off in seen or off <= 0 or off + 2 > len(data):
            continue
        seen.add(off)
        try:
            tags, nxt = _ifd(data, off, e)
        except (ValueError, struct.error):
            continue
        if nxt:
            todo.append(nxt)
        if 0x014A in tags:
            todo.extend(_ints(tags[0x014A], e))
        for o_tag, l_tag in ((0x0201, 0x0202), (0x0111, 0x0117)):
            if o_tag in tags and l_tag in tags:
                o, l = _ints(tags[o_tag], e), _ints(tags[l_tag], e)
                if len(o) == 1 and len(l) == 1:
                    cands.append((o[0], l[0]))
        if 0x002E in tags:  # Panasonic JpgFromRaw
            typ, count, val = tags[0x002E]
            p = data.find(val[:64]) if count > 4 else -1
            if p > 0:
                cands.append((p, count))
    return [(o, l) for o, l in cands if o + l <= size and _is_viewable_jpeg(f, o, l)]


def _cr3_previews(f):
    """{"full": (off, len), "medium": ..., "thumb": ...} for a Canon CR3."""
    a, b, end = _cr3_moov(f)
    out = {}
    traks = [(ta, tb) for typ, ta, tb in _bmff_boxes(f, a, b) if typ == b"trak"]
    if traks:
        def child(s, e, name):
            for typ, ca, cb in _bmff_boxes(f, s, e):
                if typ == name:
                    return ca, cb
            return None
        node = traks[0]
        for name in (b"mdia", b"minf", b"stbl"):
            node = child(node[0], node[1], name) if node else None
        if node:
            stsz = child(node[0], node[1], b"stsz")
            stco, co64 = child(node[0], node[1], b"stco"), child(node[0], node[1], b"co64")
            if stsz and (stco or co64):
                f.seek(stsz[0] + 4)  # skip version/flags
                sample_size, _count = struct.unpack(">II", f.read(8))
                if not sample_size:
                    (sample_size,) = struct.unpack(">I", f.read(4))
                f.seek((stco or co64)[0] + 8)  # skip version/flags and entry count
                (off,) = struct.unpack(">I", f.read(4)) if stco else struct.unpack(">Q", f.read(8))
                if _is_viewable_jpeg(f, off, sample_size):
                    out["full"] = (off, sample_size)
    for typ, ua, ub in _bmff_boxes(f, 0, end):
        if typ == b"uuid":
            f.seek(ua)
            blob = f.read(min(ub - ua, 2 * 1024 * 1024))
            p = blob.find(b"PRVW")
            if p >= 0:
                s = blob.find(b"\xff\xd8\xff", p)
                if s >= 0:
                    out["medium"] = (ua + s, len(blob) - s)
        if typ == b"moov":
            f.seek(ua)
            blob = f.read(ub - ua)
            p = blob.find(b"THMB")
            if p >= 0:
                s = blob.find(b"\xff\xd8\xff", p)
                (box_len,) = struct.unpack(">I", blob[p - 4:p])
                if s >= 0:
                    out["thumb"] = (ua + s, p - 4 + box_len - s)
    return out


PREVIEW_CACHE = os.path.join(tempfile.gettempdir(), "caption-images-previews")


USE_SIPS = sys.platform == "darwin" and os.path.exists("/usr/bin/sips")


def pillow():
    """Pillow (the Python image library) if it's installed, with HEIC support when pillow-heif is too.
    Windows uses it for previews and smaller copies; the Mac uses its built-in sips instead."""
    try:
        from PIL import Image, ImageOps
    except ImportError:
        return None
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
    except Exception:
        pass
    return Image, ImageOps


def _render_preview(path, max_px):
    """A preview for photos with no embedded JPEG (HEIC, PNG, unusual RAWs)."""
    return _sips_preview(path, max_px) if USE_SIPS else _pillow_preview(path, max_px)


def _cache_path(path, max_px):
    st = os.stat(path)
    key = hashlib.sha1(("%s|%d|%d|%d" % (path, st.st_size, int(st.st_mtime), max_px)).encode()).hexdigest()
    return os.path.join(PREVIEW_CACHE, key + ".jpg")


def _pillow_preview(path, max_px):
    pil = pillow()
    if not pil:
        return None
    Image, ImageOps = pil
    out = _cache_path(path, max_px)
    if not os.path.isfile(out):
        try:
            with Image.open(path) as im:
                im = ImageOps.exif_transpose(im)
                im.thumbnail((max_px, max_px))
                if im.mode not in ("RGB", "L"):
                    im = im.convert("RGB")
                os.makedirs(PREVIEW_CACHE, exist_ok=True)
                tmp = out + ".tmp.jpg"
                im.save(tmp, "JPEG", quality=80)
            os.replace(tmp, out)
        except Exception:  # not an image Pillow can read (most RAWs without a preview)
            return None
    with open(out, "rb") as f:
        return f.read()


def _sips_preview(path, max_px):
    """Render a preview with macOS sips (slow, but works for HEIC and unusual RAWs)."""
    if not USE_SIPS:
        return None
    out = _cache_path(path, max_px)
    if not os.path.isfile(out):
        os.makedirs(PREVIEW_CACHE, exist_ok=True)
        tmp = out + ".tmp.jpg"
        r = subprocess.run(["sips", "-s", "format", "jpeg", "-s", "formatOptions", "80", "-Z", str(max_px), path, "--out", tmp],
                           capture_output=True)
        if r.returncode != 0 or not os.path.isfile(tmp):
            return None
        os.replace(tmp, out)
    with open(out, "rb") as f:
        return f.read()


SIZES_PX = {"thumb": 320, "screen": 2000}  # thumbnail strip; main Cull view (the size photos go on the website)
_RESIZE_SLOTS = threading.BoundedSemaphore(max(2, (os.cpu_count() or 4) - 1))  # don't start 50 sips at once


def _jpeg_dims(data):
    """(width, height) from a JPEG's header, or (0, 0)."""
    i = 2
    while i + 9 < len(data) and data[i] == 0xFF:
        m = data[i + 1]
        (seg,) = struct.unpack(">H", data[i + 2:i + 4])
        if m in (0xC0, 0xC1, 0xC2):
            h, w = struct.unpack(">HH", data[i + 5:i + 9])
            return w, h
        i += 2 + seg
    return 0, 0


def _resized(path, max_px, data=None):
    """JPEG bytes no bigger than max_px on the long side, made once and kept in the preview cache.
    data: JPEG bytes to shrink (a RAW's built-in preview); None shrinks the file at path itself.
    The orientation is left to the caller. None if this computer can't resize (no sips, no Pillow)."""
    out = _cache_path(path, max_px).replace(".jpg", "-r.jpg")
    if os.path.isfile(out):
        os.utime(out)  # recently used: kept when the cache is trimmed
        with open(out, "rb") as f:
            return f.read()
    if not USE_SIPS and not pillow():
        return None
    os.makedirs(PREVIEW_CACHE, exist_ok=True)
    fd, tmp = tempfile.mkstemp(suffix=".jpg", dir=PREVIEW_CACHE, prefix=".r-")
    os.close(fd)
    src_tmp = None
    try:
        with _RESIZE_SLOTS:
            if USE_SIPS:
                src = path
                if data is not None:
                    fd, src_tmp = tempfile.mkstemp(suffix=".jpg", dir=PREVIEW_CACHE, prefix=".s-")
                    with os.fdopen(fd, "wb") as f:
                        f.write(data)
                    src = src_tmp
                r = subprocess.run(["sips", "-s", "format", "jpeg", "-s", "formatOptions", "82", "-Z", str(max_px), src, "--out", tmp],
                                   capture_output=True)
                if r.returncode != 0 or not os.path.getsize(tmp):
                    return None
            else:
                import io
                Image, _ = pillow()
                with Image.open(io.BytesIO(data) if data is not None else path) as im:
                    im.draft("RGB", (max_px, max_px))  # fast: JPEGs decode straight at a smaller size
                    im.thumbnail((max_px, max_px), Image.LANCZOS)
                    if im.mode not in ("RGB", "L"):
                        im = im.convert("RGB")
                    im.save(tmp, "JPEG", quality=82)
        os.replace(tmp, out)
        tmp = None
        with open(out, "rb") as f:
            return f.read()
    except Exception:
        return None
    finally:
        for t in (tmp, src_tmp):
            if t and os.path.exists(t):
                os.unlink(t)


def prune_previews(max_age_days=14, max_bytes=3 * 1024 ** 3):
    """Keep the preview cache from growing forever: drop old entries, then the least recently used."""
    try:
        files = [os.path.join(PREVIEW_CACHE, n) for n in os.listdir(PREVIEW_CACHE)]
    except OSError:
        return
    now, keep = time.time(), []
    for p in files:
        try:
            st = os.stat(p)
        except OSError:
            continue
        if now - st.st_mtime > max_age_days * 86400:
            try:
                os.unlink(p)
            except OSError:
                pass
        else:
            keep.append((st.st_mtime, st.st_size, p))
    total = sum(k[1] for k in keep)
    for _, size, p in sorted(keep):
        if total <= max_bytes:
            break
        try:
            os.unlink(p)
            total -= size
        except OSError:
            pass


def preview(path, size="medium"):
    """JPEG bytes to show for a photo, or None if the file can be shown as-is.

    size: "thumb" (small, for strips), "medium" (screen-sized), "full" (for 100% zoom).
    """
    ext = ext_of(path)
    if ext in (".jpg", ".jpeg") and size == "thumb":
        small = _resized(path, SIZES_PX["thumb"])
        if small:
            return _orient_jpeg(small, capture_info(path)["orientation"])
    if size == "screen" and ext in (".jpg", ".jpeg", ".png"):
        return None  # shown as-is: already full quality
    if ext in (".jpg", ".jpeg"):
        if size != "thumb":
            return None
        with open(path, "rb") as f:
            segs = _jpeg_header(f)
        i = _find(segs, 0xE1, b"Exif\x00\x00")
        if i is not None:  # the camera's embedded thumbnail
            tiff = segs[i][1][6:]
            try:
                e = _tiff_endian(tiff)
                ifd0, nxt = _ifd(tiff, struct.unpack(e + "I", tiff[4:8])[0], e)
                if nxt:
                    ifd1, _ = _ifd(tiff, nxt, e)
                    o, l = _ints(ifd1.get(0x0201, (4, 0, b"")), e), _ints(ifd1.get(0x0202, (4, 0, b"")), e)
                    if o and l and tiff[o[0]:o[0] + 2] == b"\xff\xd8":
                        orient = _ints(ifd0[0x0112], e)[0] if 0x0112 in ifd0 else 1
                        return _orient_jpeg(tiff[o[0]:o[0] + l[0]], orient)
            except (ValueError, struct.error, IndexError):
                pass
        return _render_preview(path, 400)
    if ext == ".png":
        return None if size != "thumb" else _render_preview(path, 400)

    orient = capture_info(path)["orientation"]
    region = None
    try:
        with open(path, "rb") as f:
            if ext == ".cr3":
                p = _cr3_previews(f)
                order = {"thumb": ("medium", "full", "thumb"), "medium": ("medium", "full"),
                         "screen": ("full", "medium"), "full": ("full", "medium")}[size]
                region = next((p[k] for k in order if k in p), None)
            elif ext == ".raf":
                f.seek(84)
                o, l = struct.unpack(">II", f.read(8))
                region = (o, l) if _is_viewable_jpeg(f, o, l) else None
            elif ext in RAW_EXTS or ext in (".tif", ".tiff"):
                cands = sorted(_tiff_previews(f), key=lambda c: c[1])
                if cands:
                    # the smallest one that's still sharp enough for the strip
                    region = next((c for c in cands if c[1] > 40000), cands[-1]) if size == "thumb" else cands[-1]
            if region:
                f.seek(region[0])
                data = f.read(region[1])
                want = SIZES_PX.get(size)
                if want and max(_jpeg_dims(data)) > want:
                    small = _resized(path, want, data)
                    if small:
                        data = small
                return _orient_jpeg(data, orient)
    except (OSError, ValueError, struct.error):
        pass
    return _render_preview(path, SIZES_PX.get(size, 2560))


# ------------------------------------------------------- smaller copies ----

ROTATE_FOR = {3: 180, 6: 90, 8: 270}  # EXIF orientation -> clockwise turn that makes it upright


def _image_size(path):
    r = subprocess.run(["sips", "-g", "pixelWidth", "-g", "pixelHeight", path], capture_output=True, text=True)
    w = re.search(r"pixelWidth:\s*(\d+)", r.stdout)
    h = re.search(r"pixelHeight:\s*(\d+)", r.stdout)
    return (int(w.group(1)), int(h.group(1))) if w and h else (0, 0)


def _set_orientation_up(path):
    """After turning the pixels upright, mark the file as upright (so viewers don't turn it again)."""
    with open(path, "rb") as f:
        data = f.read()
    try:
        segs, rest = _jpeg_split(data)
    except ValueError:
        return
    i = _find(segs, 0xE1, b"Exif\x00\x00")
    if i is None:
        return
    tiff = bytearray(segs[i][1][6:])
    try:
        e = _tiff_endian(tiff)
        off = struct.unpack(e + "I", tiff[4:8])[0]
        (n,) = struct.unpack(e + "H", tiff[off:off + 2])
        for k in range(n):
            p = off + 2 + 12 * k
            if struct.unpack(e + "H", tiff[p:p + 2])[0] == 0x0112:
                tiff[p + 8:p + 10] = struct.pack(e + "H", 1)
    except (ValueError, struct.error):
        return
    segs[i][1] = b"Exif\x00\x00" + bytes(tiff)
    _atomic_write(path, _jpeg_join(segs, rest))


def export_available():
    return USE_SIPS or pillow() is not None


def make_smaller(src_jpeg, out_path, long_edge=0, quality=85, max_bytes=0):
    """Write an upright JPEG copy of src_jpeg, at most long_edge pixels on its long side (0 = full size),
    and, if max_bytes is set, under that size (lowering quality, then size, as needed).
    Uses sips on a Mac, Pillow elsewhere."""
    if not USE_SIPS:
        return _make_smaller_pillow(src_jpeg, out_path, long_edge, quality, max_bytes)
    turn = ROTATE_FOR.get(capture_info(src_jpeg)["orientation"], 0)
    w, h = _image_size(src_jpeg)
    edge = long_edge if long_edge and long_edge < max(w, h) else 0
    q = quality
    for _ in range(12):
        args = ["sips", "-s", "format", "jpeg", "-s", "formatOptions", str(q)]
        if turn:
            args += ["-r", str(turn)]
        if edge:
            args += ["-Z", str(edge)]
        r = subprocess.run(args + [src_jpeg, "--out", out_path], capture_output=True, text=True)
        if r.returncode != 0 or not os.path.isfile(out_path):
            raise IOError("Couldn't make a smaller copy: " + (r.stderr.strip() or "sips failed"))
        if not max_bytes or os.path.getsize(out_path) <= max_bytes:
            break
        if q > 55:  # first give up some quality, then some size
            q -= 10
        else:
            edge = int((edge or max(w, h)) * 0.85)
    if turn:
        _set_orientation_up(out_path)
    return os.path.getsize(out_path)


def set_date_created(path, when):
    """when: "YYYY:MM:DD HH:MM:SS" (EXIF style). Written as XMP photoshop:DateCreated, which Photo Mechanic shows."""
    m = re.match(r"(\d{4}):(\d\d):(\d\d) (\d\d):(\d\d):(\d\d)", when or "")
    if not m or ext_of(path) not in (".jpg", ".jpeg"):
        return
    iso = "%s-%s-%sT%s:%s:%s" % m.groups()
    with open(path, "rb") as f:
        data = f.read()
    segs, rest = _jpeg_split(data)
    i = _find(segs, 0xE1, XMP_HEADER)
    old = segs[i][1][len(XMP_HEADER):].decode("utf-8", "replace") if i is not None else None
    payload = XMP_HEADER + xmp_set(old, {"photoshop:DateCreated": iso}).encode("utf-8")
    if i is not None:
        segs[i][1] = payload
    else:
        segs.insert(_insert_pos(segs), [0xE1, payload])
    _atomic_write(path, _jpeg_join(segs, rest), like=path)


def _make_smaller_pillow(src_jpeg, out_path, long_edge=0, quality=85, max_bytes=0):
    pil = pillow()
    if not pil:
        raise IOError("Smaller copies need Pillow. Close the app and open it again to set it up.")
    Image, ImageOps = pil
    with Image.open(src_jpeg) as src:
        im = ImageOps.exif_transpose(src)  # upright pixels, orientation tag reset
        exif = im.getexif()
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        w, h = im.size
        edge = long_edge if long_edge and long_edge < max(w, h) else 0
        q = quality
        for _ in range(12):
            out = im
            if edge:
                out = im.copy()
                out.thumbnail((edge, edge), Image.LANCZOS)
            out.save(out_path, "JPEG", quality=q, exif=exif.tobytes(), optimize=True)
            if not max_bytes or os.path.getsize(out_path) <= max_bytes:
                break
            if q > 55:  # first give up some quality, then some size
                q -= 10
            else:
                edge = int((edge or max(w, h)) * 0.85)
