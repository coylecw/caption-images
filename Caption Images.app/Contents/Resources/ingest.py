"""Ingest (copy photos off a memory card) and file jobs for culling.

Nothing here ever deletes or changes files on the card.
"""
import datetime
import json
import os
import re
import shutil
import tempfile
import string
import sys
import threading

import imgmeta

LOG_PATH = os.path.join(os.path.expanduser("~"), ".caption-images-ingested.json")
SKIP_DIRS = {".Trashes", ".Spotlight-V100", ".fseventsd", ".TemporaryItems", "System Volume Information", "$RECYCLE.BIN"}
CHUNK = 4 * 1024 * 1024


# ------------------------------------------------------------- finding ----

def find_cards():
    """Mounted drives that look like camera cards (they have a DCIM folder)."""
    if sys.platform == "darwin":
        roots = [os.path.join("/Volumes", v) for v in sorted(os.listdir("/Volumes"))]
    elif os.name == "nt":
        roots = ["%s:\\" % c for c in string.ascii_uppercase[3:]]
    else:
        user = os.environ.get("USER", "")
        roots = []
        for base in ("/media/" + user, "/run/media/" + user, "/media"):
            if os.path.isdir(base):
                roots += [os.path.join(base, v) for v in sorted(os.listdir(base))]
    cards = []
    for r in roots:
        try:
            if os.path.isdir(os.path.join(r, "DCIM")):
                cards.append({"name": os.path.basename(r.rstrip("/\\")) or r, "path": r})
        except OSError:
            pass
    return cards


def parse_time(text):
    try:
        return datetime.datetime.strptime(text.strip()[:19], "%Y:%m:%d %H:%M:%S")
    except (ValueError, AttributeError):
        return None


def scan(source):
    """Group the photos under source into shots (RAW+JPEG with the same name = one shot), oldest first."""
    groups, sidecars = {}, {}
    for root, dirs, names in os.walk(source):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in SKIP_DIRS)
        for n in names:
            if n.startswith("."):
                continue
            if imgmeta.ext_of(n) == ".xmp":
                sidecars[(root, os.path.splitext(n)[0].lower())] = os.path.join(root, n)
                continue
            if imgmeta.ext_of(n) not in imgmeta.PHOTO_EXTS:
                continue
            p = os.path.join(root, n)
            try:
                st = os.stat(p)
            except OSError:
                continue
            f = {"path": p, "name": n, "size": st.st_size, "mtime": st.st_mtime}
            groups.setdefault((root, os.path.splitext(n)[0].lower()), []).append(f)
    shots = []
    for key, files in groups.items():
        files.sort(key=lambda f: f["name"].lower())
        t = None
        for f in sorted(files, key=lambda f: imgmeta.ext_of(f["name"]) not in (".jpg", ".jpeg")):
            t = parse_time(imgmeta.capture_info(f["path"])["time"] or "")
            if t:
                break
        if not t:
            t = datetime.datetime.fromtimestamp(min(f["mtime"] for f in files))
        rating = 0  # set while culling on the card; a value in either file of a pair counts
        for f in files:
            try:
                rating = rating or imgmeta.read_meta(f["path"])["rating"]
            except Exception:
                pass
        shots.append({"files": files, "time": t, "stem": os.path.splitext(files[0]["name"])[0],
                      "rating": rating, "sidecar": sidecars.get(key)})
    shots.sort(key=lambda s: (s["time"], s["files"][0]["name"].lower()))
    return shots


# ---------------------------------------------------------------- log ----

def _key(f):
    return "%s|%d|%d" % (f["name"], f["size"], int(f["mtime"]))


def load_log():
    try:
        with open(LOG_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_log(log):
    tmp = LOG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(log, fh)
    os.replace(tmp, LOG_PATH)


def already_ingested(log, f):
    """True if this exact card file was copied before and that copy still exists."""
    return any(os.path.exists(p) for p in log.get(_key(f), []))


def summary(source, shots):
    log = load_log()
    exts, total, done = {}, 0, 0
    ratings = {}
    for s in shots:
        ratings[s["rating"]] = ratings.get(s["rating"], 0) + 1
        for f in s["files"]:
            e = imgmeta.ext_of(f["name"])[1:].upper()
            exts[e] = exts.get(e, 0) + 1
            total += f["size"]
        s["already"] = all(already_ingested(log, f) for f in s["files"])
        done += s["already"]
    return {
        "source": source,
        "shots": len(shots),
        "files": sum(len(s["files"]) for s in shots),
        "bytes": total,
        "types": exts,
        "already": done,
        "rated": sum(v for k, v in ratings.items() if k > 0),
        "rejected": ratings.get(-1, 0),
        "flags": [[s["rating"], s["already"]] for s in shots],  # lets the page count what each choice would copy
        "first": shots[0]["time"].isoformat() if shots else None,
        "last": shots[-1]["time"].isoformat() if shots else None,
        "sample": [{"time": s["time"].isoformat(), "stem": s["stem"], "files": [f["name"] for f in s["files"]]} for s in shots[:3]],
    }


# -------------------------------------------------------------- naming ----

BAD_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def safe_name(s):
    s = BAD_CHARS.sub("", s)
    return re.sub(r"\s+", " ", s).strip().strip(".")


def render(template, when, event, num, original, initials, for_file):
    ev = re.sub(r"\s+", "-", event.strip()) if for_file else event.strip()
    vals = {
        "date": when.strftime("%Y%m%d"), "isodate": when.strftime("%Y-%m-%d"), "time": when.strftime("%H%M%S"),
        "event": ev, "num": "%04d" % num, "original": original, "initials": initials,
    }
    out = re.sub(r"\{(\w+)\}", lambda m: vals.get(m.group(1).lower(), m.group(0)), template)
    if for_file:
        out = re.sub(r"([_\-. ])[_\-. ]+", r"\1", out)  # collapse separators left by empty fields
        out = out.strip("_-. ")
    return safe_name(out)


def wanted(shot, only):
    """only: "all", "norejects", or a minimum star rating ("1"-"5")."""
    if only == "norejects":
        return shot["rating"] != -1
    if only.isdigit() and int(only) > 0:
        return shot["rating"] >= int(only)
    return True


def initials_of(name):
    return "".join(w[0] for w in re.findall(r"[A-Za-z]+", name)).upper()


def unique_path(path):
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    n = 2
    while os.path.exists("%s-%d%s" % (base, n, ext)):
        n += 1
    return "%s-%d%s" % (base, n, ext)


# ---------------------------------------------------------------- jobs ----

class Job:
    """A background copy job the page polls for progress."""

    def __init__(self, kind):
        self.kind = kind
        self.state = {"kind": kind, "running": True, "finished": False, "cancelled": False, "files_total": 0,
                      "files_done": 0, "copied": 0, "skipped": 0, "bytes_total": 0, "bytes_done": 0,
                      "current": "", "dest": "", "errors": []}
        self.cancel = threading.Event()

    def start(self, fn, *args):
        def run():
            try:
                fn(self, *args)
            except Exception as e:  # report instead of dying silently
                self.state["errors"].append(str(e))
            finally:
                self.state["running"] = False
                self.state["finished"] = True
                self.state["cancelled"] = self.cancel.is_set()
        threading.Thread(target=run, daemon=True).start()
        return self

    def copy(self, src, dest):
        """Copy with progress; the file only appears under its real name once complete and verified."""
        tmp = os.path.join(os.path.dirname(dest), ".ingesting-" + os.path.basename(dest))
        size = os.path.getsize(src)
        try:
            with open(src, "rb") as fi, open(tmp, "wb") as fo:
                while True:
                    if self.cancel.is_set():
                        raise InterruptedError("Cancelled")
                    buf = fi.read(CHUNK)
                    if not buf:
                        break
                    fo.write(buf)
                    self.state["bytes_done"] += len(buf)
            if os.path.getsize(tmp) != size:
                raise IOError("Copy of %s is incomplete" % os.path.basename(src))
            shutil.copystat(src, tmp)
            os.replace(tmp, dest)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise


def run_ingest(job, shots, opts):
    """opts: dest, rename, template, event, photographer, skip_done, start_num"""
    dest = opts["dest"]
    os.makedirs(dest, exist_ok=True)
    job.state["dest"] = dest
    log = load_log()
    work = []
    only = str(opts.get("only") or "all")
    job.state["left_out"] = sum(1 for s in shots if not wanted(s, only))
    shots = [s for s in shots if wanted(s, only)]
    for s in shots:
        todo = [f for f in s["files"] if not (opts["skip_done"] and already_ingested(log, f))]
        job.state["skipped"] += len(s["files"]) - len(todo)
        if todo:
            work.append((s, todo))
    job.state["files_total"] = sum(len(t) for _, t in work)
    job.state["bytes_total"] = sum(f["size"] for _, t in work for f in t)
    initials = initials_of(opts.get("photographer", ""))
    num = max(1, int(opts.get("start_num") or 1))
    try:
        for shot, todo in work:
            if job.cancel.is_set():
                break
            if opts["rename"]:
                def target(f, n):
                    stem = render(opts["template"], shot["time"], opts["event"], n, shot["stem"], initials, True) or shot["stem"]
                    return os.path.join(dest, stem + imgmeta.ext_of(f["name"]))
                while any(os.path.exists(target(f, num)) for f in todo):
                    num += 1
                targets = [target(f, num) for f in todo]
                num += 1
            else:
                targets = [unique_path(os.path.join(dest, f["name"])) for f in todo]
            for f, t in zip(todo, targets):
                job.state["current"] = "%s → %s" % (f["name"], os.path.basename(t))
                try:
                    job.copy(f["path"], t)
                except InterruptedError:
                    break
                except Exception as e:
                    job.state["errors"].append("%s: %s" % (f["name"], e))
                    job.state["files_done"] += 1
                    continue
                log.setdefault(_key(f), []).append(t)
                sc = shot.get("sidecar")
                if sc and imgmeta.ext_of(t) in imgmeta.SIDECAR_EXTS:
                    sc_dest = imgmeta.sidecar_path(t)
                    if not os.path.exists(sc_dest):  # ratings and captions made on the card come along
                        try:
                            shutil.copy2(sc, sc_dest)
                        except Exception as e:
                            job.state["errors"].append("%s: %s" % (os.path.basename(sc), e))
                try:
                    apply_template(t, opts, shot, num - 1 if opts["rename"] else job.state["copied"] + 1, initials)
                except Exception as e:
                    job.state["errors"].append("%s: copied, but the metadata couldn't be added (%s)" % (os.path.basename(t), e))
                job.state["copied"] += 1
                job.state["files_done"] += 1
    finally:
        save_log(log)
        job.state["current"] = ""


def apply_template(path, opts, shot, num, initials):
    """Photographer plus the metadata template. Fill-ins like {date} and {event} work in every field.
    Caption and photographer only fill blanks (a caption written on the card stays); keywords are added;
    the other template fields replace what the camera wrote."""
    name = opts.get("photographer", "").strip()
    tpl = opts.get("meta") or {}
    when, event = shot["time"], opts.get("event", "")
    plain = lambda v: render_text(str(v), when, event, num, shot["stem"], initials).strip()  # noqa: E731
    # Getty-style pieces built from the template's own fields (see render_text)
    getty = {"city": plain(tpl.get("city", "")), "state": plain(tpl.get("state", "")),
             "venue": plain(tpl.get("location", "")), "credit": plain(tpl.get("credit", "")), "photographer": name}
    fill = lambda v: render_text(str(v), when, event, num, shot["stem"], initials, getty)  # noqa: E731
    extra = {k: fill(v).strip(" /") for k, v in tpl.items() if k in imgmeta.TEMPLATE_FIELDS and str(v).strip()}  # " /": no name typed
    extra = {k: v for k, v in extra.items() if v}
    if extra.get("location", "").lower().startswith("the "):  # "the Stroh Center" reads well in a caption; the field is just the name
        extra["location"] = extra["location"][4:].strip()
    caption = fill(tpl.get("caption", "")).strip()
    if caption and tpl.get("getty"):
        caption = getty_caption(caption, when, getty["city"], getty["state"], getty["venue"], name, getty["credit"])
    keywords = [fill(k).strip() for k in (tpl.get("keywords") or [])]
    keywords = [k for k in keywords if k]
    if not (name or extra or caption or keywords):
        return
    have = imgmeta.read_meta(path)
    kw = {"extra": extra}
    if name and not have["creator"]:
        kw["creator"] = name
    if caption and not have["caption"].strip():
        kw["caption"] = caption
    if keywords:
        kw["keywords"] = have["keywords"] + keywords
    if have["rating"]:  # stars from the camera: written out too, so Lightroom and Photo Mechanic see them
        kw["rating"] = have["rating"]
    imgmeta.write_meta(path, **kw)


DATELINE_RE = re.compile(r"^[A-Z][A-Z .,'-]* - [A-Z]+ \d{1,2}:")


def getty_caption(body, when, city, state, venue, photographer, credit):
    """BG Falcon Media / Getty style caption around a plain sentence:
    BOWLING GREEN, OHIO - NOVEMBER 05: <sentence> on November 5, 2026 at the Stroh Center in Bowling Green, Ohio. (Name/Credit)
    Parts already written in the sentence (dateline, date, credit) aren't added twice."""
    body = " ".join(body.split())
    written_by = re.search(r"\s*\([^()]+/[^()]+\)\s*$", body)  # a credit already typed at the end
    if written_by:
        body = body[:written_by.start()]
    body = body.rstrip(". ")
    place = ", ".join(x for x in (city.strip(), state.strip()) if x)
    month = when.strftime("%B")
    head = "" if DATELINE_RE.match(body) else ((place.upper() + " - ") if place else "") + "%s %02d: " % (month.upper(), when.day)
    has_date = re.search(r"%s \d{1,2}, \d{4}" % month, body)
    tail = "" if has_date else " on %s %d, %d" % (month, when.day, when.year) + (" at " + venue.strip() if venue.strip() else "") + (" in " + place if place else "")
    who, credit = photographer.strip(), credit.strip()
    by = written_by.group().strip() if written_by else ("(%s/%s)" % (who, credit) if who and credit else "(%s)" % who if who else "")
    return head + body + tail + "." + (" " + by if by else "")


def render_text(template, when, event, num, original, initials, getty=None):
    """Fill-ins for metadata text: keeps line breaks and punctuation that file names can't have.
    getty: {"city", "state", "venue", "credit", "photographer"} for the Getty Images caption pieces:
      {dateline}  BOWLING GREEN, OHIO - NOVEMBER 05:     {gettydate}  November 05, 2026
      {place}     Bowling Green, Ohio                    {venue}      Stroh Center
      {photoby}   (Photo by Jane Smith/BGSU Athletics)"""
    g = getty or {}
    month_day = "%s %02d" % (when.strftime("%B"), when.day)
    place = ", ".join(x for x in (g.get("city", ""), g.get("state", "")) if x)
    who, credit = g.get("photographer", ""), g.get("credit", "")
    vals = {
        "date": when.strftime("%Y%m%d"), "isodate": when.strftime("%Y-%m-%d"), "time": when.strftime("%H%M%S"),
        "year": when.strftime("%Y"), "longdate": "%s %d, %s" % (when.strftime("%B"), when.day, when.year),
        "event": event.strip(), "num": "%04d" % num, "original": original, "initials": initials,
        "dateline": ((place.upper() + " - ") if place else "") + month_day.upper() + ":",
        "gettydate": "%s, %d" % (month_day, when.year),
        "place": place, "venue": g.get("venue", ""), "photographer": who,
        "photoby": ("(Photo by %s/%s)" % (who, credit) if who and credit else "(Photo by %s)" % who if who else ""),
    }
    return re.sub(r"\{(\w+)\}", lambda m: vals.get(m.group(1).lower(), m.group(0)), template)


def related_files(folder, names):
    """The photo files plus any sidecars that share their names."""
    out = []
    for n in names:
        out.append(n)
        sc = os.path.splitext(n)[0] + ".xmp"
        if imgmeta.ext_of(n) in imgmeta.SIDECAR_EXTS and os.path.isfile(os.path.join(folder, sc)) and sc not in out:
            out.append(sc)
    return out


def run_copy(job, folder, names, dest, move=False, lock=None):
    """Copy (or move) photos and their sidecars into dest. Moving never overwrites anything."""
    job.state["dest"] = dest
    job.state["move"] = move
    job.state["moved"] = 0
    os.makedirs(dest, exist_ok=True)
    files = [n for n in related_files(folder, names) if os.path.isfile(os.path.join(folder, n))]
    job.state["files_total"] = len(files)
    job.state["bytes_total"] = sum(os.path.getsize(os.path.join(folder, n)) for n in files)
    same_disk = move and os.stat(folder).st_dev == os.stat(dest).st_dev
    # a RAW and its .xmp must land under matching names, so pick targets per photo
    targets, used = {}, set()
    for n in files:
        stem = os.path.splitext(n)[0]
        if stem in targets:
            continue
        base = os.path.splitext(os.path.basename(n))[0]
        group = [m for m in files if os.path.splitext(m)[0] == stem]
        k, cand = 1, base
        while move and (cand.lower() in used or any(os.path.exists(os.path.join(dest, cand + os.path.splitext(m)[1])) for m in group)):
            k += 1
            cand = "%s-%d" % (base, k)
        used.add(cand.lower())
        targets[stem] = cand
    for n in files:
        if job.cancel.is_set():
            break
        job.state["current"] = n
        src = os.path.join(folder, n)
        target = os.path.join(dest, targets[os.path.splitext(n)[0]] + os.path.splitext(n)[1])
        try:
            if move:
                size = os.path.getsize(src)
                if lock:
                    lock.acquire()
                try:
                    if same_disk:
                        os.rename(src, target)
                        job.state["bytes_done"] += size
                    else:
                        job.copy(src, target)
                        os.remove(src)
                finally:
                    if lock:
                        lock.release()
                job.state["moved"] += 1
            elif os.path.exists(target) and os.path.getsize(target) == os.path.getsize(src) and not n.endswith(".xmp"):
                job.state["skipped"] += 1
                job.state["bytes_done"] += os.path.getsize(target)
            else:
                job.copy(src, target)
                job.state["copied"] += 1
        except InterruptedError:
            break
        except Exception as e:
            job.state["errors"].append("%s: %s" % (n, e))
        job.state["files_done"] += 1
    job.state["current"] = ""


def move_to_subfolder(folder, names, sub="Rejects"):
    dest = os.path.join(folder, sub)
    os.makedirs(dest, exist_ok=True)
    moved = 0
    for n in related_files(folder, names):
        src = os.path.join(folder, n)
        if os.path.isfile(src):
            shutil.move(src, unique_path(os.path.join(dest, os.path.basename(n))))
            moved += 1
    return dest, moved


def run_export(job, folder, shots, dest, opts):
    """Smaller JPEG copies of each shot in dest, with caption, credit, rating and tags.
    shots: [{"files": [relative names], "time": "...", "meta": {...}}]
    opts: long_edge (px, 0 = full size), quality (1-100), max_mb (0 = no limit)."""
    job.state.update(dest=dest, export=True, files_total=len(shots), bytes_total=len(shots), made=0, made_bytes=0)
    os.makedirs(dest, exist_ok=True)
    max_bytes = int(float(opts.get("max_mb") or 0) * 1024 * 1024)
    for shot in shots:
        if job.cancel.is_set():
            break
        files = shot["files"]
        # a camera JPEG if there is one, otherwise the full-size preview inside the RAW
        main = next((n for n in files if imgmeta.ext_of(n) in (".jpg", ".jpeg")), files[0])
        job.state["current"] = main
        tmp = None
        try:
            src = os.path.join(folder, main)
            if imgmeta.ext_of(main) not in (".jpg", ".jpeg"):
                data = imgmeta.preview(src, "full")
                if data:
                    fd, tmp = tempfile.mkstemp(suffix=".jpg", prefix="caption-images-")
                    with os.fdopen(fd, "wb") as f:
                        f.write(data)
                    src = tmp
            out = unique_path(os.path.join(dest, os.path.splitext(os.path.basename(main))[0] + ".jpg"))
            imgmeta.make_smaller(src, out, int(opts.get("long_edge") or 0), int(opts.get("quality") or 85), max_bytes)
            m = shot.get("meta") or {}
            try:  # copyright, credit, city... from the metadata template, read off the original
                extra = {k: v for k, v in imgmeta.read_template_fields(os.path.join(folder, main)).items() if v}
            except Exception:
                extra = {}
            imgmeta.write_meta(out, caption=m.get("caption") or "", creator=m.get("creator") or "",
                               rating=m.get("rating") or 0, keywords=m.get("keywords") or [], people=m.get("people") or [],
                               extra=extra)
            imgmeta.set_date_created(out, shot.get("time"))
            job.state["made"] += 1
            job.state["copied"] += 1
            job.state["made_bytes"] += os.path.getsize(out)
        except Exception as e:
            job.state["errors"].append("%s: %s" % (main, e))
        finally:
            if tmp:
                os.unlink(tmp)
        job.state["files_done"] += 1
        job.state["bytes_done"] += 1
    job.state["current"] = ""
