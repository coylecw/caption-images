#!/usr/bin/env python3
"""Caption Images: ingest photos from a card, cull them, and caption them.

Usage:
    python3 caption.py [folder]

The app runs in your web browser and only listens on this computer
(127.0.0.1). Press Ctrl+C in the terminal to quit.

  Ingest   copy photos off a memory card into a new folder, renamed, with your name added
  Cull     go through photos fast: 1-5 stars, X to reject, Z to zoom
  Caption  write captions, with Photo Mechanic-style code replacements
  Teach numbers  label jerseys so it reads your teams' numbers better (Mac)

Captions, names and ratings go into JPEG/PNG files directly, and into
Lightroom-style .xmp sidecars for RAW files. RAW files are never modified.
Code replacement files live in the "Code Replacements" folder next to the app.
"""
import json
import mimetypes
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APP_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, APP_DIR)

# Launched with pythonw (no console window), so there's nowhere for a startup problem to show
# up - stdout/stderr are discarded (or even None). Send them to a log file instead, before
# anything else runs, so an import-time failure or any other silent crash leaves a trace.
if sys.platform == "win32":
    try:
        _log_path = os.path.join(tempfile.gettempdir(), "caption-images.log")
        _log = open(_log_path, "a", buffering=1, encoding="utf-8")
        _log.write("\n--- %s ---\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
        sys.stdout = sys.stderr = _log
    except Exception:
        pass  # worst case, back to no log - not worth failing startup over

try:
    import imgmeta
    import ingest
    import teach
    import jersey_numbers_win
except Exception as _e:
    print("Caption Images couldn't start: a required file is missing or broken (%s)" % _e, flush=True)
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, "Caption Images couldn't start: %s" % _e, "Caption Images", 0x10)
        except Exception:
            pass
    raise

TOKEN = secrets.token_urlsafe(24)
FOLDER = ""



def _original_path(path):
    """macOS runs a downloaded app from a hidden temporary copy ("App Translocation").
    Ask macOS where the real one is, so we can find the folders next to it."""
    if sys.platform != "darwin" or "/AppTranslocation/" not in path:
        return path
    try:
        import ctypes
        cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        sec = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
        cf.CFURLCreateFromFileSystemRepresentation.restype = ctypes.c_void_p
        cf.CFURLCreateFromFileSystemRepresentation.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_bool]
        cf.CFURLGetFileSystemRepresentation.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_char_p, ctypes.c_long]
        cf.CFRelease.argtypes = [ctypes.c_void_p]
        sec.SecTranslocateCreateOriginalPathForURL.restype = ctypes.c_void_p
        sec.SecTranslocateCreateOriginalPathForURL.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        raw = path.encode("utf-8")
        url = cf.CFURLCreateFromFileSystemRepresentation(None, raw, len(raw), True)
        orig = sec.SecTranslocateCreateOriginalPathForURL(url, None)
        cf.CFRelease(url)
        if not orig:
            return path
        buf = ctypes.create_string_buffer(4096)
        ok = cf.CFURLGetFileSystemRepresentation(orig, True, buf, 4096)
        cf.CFRelease(orig)
        return buf.value.decode("utf-8") if ok else path
    except Exception:
        return path


def _home_dir():
    """The folder the user sees: next to "Caption Images.app", or next to caption.py when run directly."""
    marker = os.sep + "Contents" + os.sep + "Resources"
    if APP_DIR.endswith(".app" + marker):
        bundle = _original_path(APP_DIR[: -len(marker)])
        return os.path.dirname(bundle)
    return APP_DIR


HOME_DIR = _home_dir()
CODES_DIR = os.path.join(HOME_DIR, "Code Replacements")
TEMPLATES_DIR = os.path.join(HOME_DIR, "Metadata Templates")  # one .json file per saved metadata template
TAGS_FILE = os.path.join(HOME_DIR, "Tags.txt")  # your own tag list: one tag per line, "#" lines are notes
PORT = 8765
APP_ID = "caption-images"
APP_VERSION = 7  # the page checks this, so an old copy still running is noticed after an update
LAST_SEEN = [time.monotonic()]  # when a page last checked in
IDLE_QUIT = 600  # seconds with no page open before the app closes itself
SERVER = None
CODE_EXTS = {".txt", ".tsv", ".tab"}
LOCK = threading.Lock()
JOB = None
SCANS = {}


# ------------------------------------------------------------ folders ----

def pick_folder(prompt="Choose a folder", start=None):
    """Show the system folder picker. Returns a path, or None if cancelled."""
    if sys.platform == "darwin":
        script = 'POSIX path of (choose folder with prompt "%s"' % prompt.replace('"', "'")
        if start and os.path.isdir(start):
            script += ' default location (POSIX file "%s")' % start.replace('"', "")
        script += ")"
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
        if r.returncode != 0:  # cancelled
            return None
        return r.stdout.strip().rstrip("/") or "/"
    try:
        import tkinter
        from tkinter import filedialog
        root = tkinter.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        path = filedialog.askdirectory(title=prompt, initialdir=start or None)
        root.destroy()
        return path or None
    except Exception:
        return None


INDEX = {}  # shot id -> photo files (paths relative to FOLDER)
FOLDER_CARD = ""  # the card FOLDER is on, remembered so we can say so once it's unplugged


def card_root(path):
    """The memory card a path is on (a drive with a DCIM folder), or None."""
    p = os.path.abspath(path)
    while True:
        if os.path.isdir(os.path.join(p, "DCIM")):
            return p
        parent = os.path.dirname(p)
        if parent == p:
            return None
        p = parent


def scan_folder():
    """Photo files in the working folder, as {shot id: [relative paths]}.

    A card (or its DCIM folder) is read with all its 100CANON, 101CANON...
    folders together; any other folder is read on its own.
    """
    groups = {}
    if not FOLDER or not os.path.isdir(FOLDER):
        return groups
    deep = os.path.isdir(os.path.join(FOLDER, "DCIM")) or os.path.basename(FOLDER).upper() == "DCIM"
    top = os.path.join(FOLDER, "DCIM") if os.path.isdir(os.path.join(FOLDER, "DCIM")) else FOLDER
    for root, dirs, names in os.walk(top):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in ingest.SKIP_DIRS) if deep else []
        for name in names:
            if name.startswith(".") or imgmeta.ext_of(name) not in imgmeta.PHOTO_EXTS:
                continue
            rel = os.path.relpath(os.path.join(root, name), FOLDER).replace(os.sep, "/")
            groups.setdefault(os.path.splitext(rel)[0].lower(), []).append(rel)
    for names in groups.values():
        names.sort(key=lambda n: (imgmeta.ext_of(n) not in imgmeta.EMBED_EXTS, n.lower()))
    return groups


def folder_shots():
    """Photos in the working folder, grouped so IMG_1.CR3 + IMG_1.JPG are one shot."""
    global INDEX
    INDEX = scan_folder()
    shots = []
    for sid, names in INDEX.items():
        meta, error = {"caption": "", "creator": "", "rating": 0, "keywords": [], "people": [], "tagged": False, "colorclass": 0}, None
        for n in names:  # combine: a value set in either file counts
            try:
                m = imgmeta.read_meta(os.path.join(FOLDER, n))
            except Exception as e:
                error = str(e)
                continue
            for k in ("caption", "creator"):
                meta[k] = meta[k] or m[k]
            meta["rating"] = meta["rating"] or m["rating"]
            meta["tagged"] = meta["tagged"] or m["tagged"]
            meta["colorclass"] = meta["colorclass"] or m["colorclass"]
            for k in ("keywords", "people"):
                meta[k] = meta[k] + [x for x in m[k] if x.lower() not in {y.lower() for y in meta[k]}]
        info = {"time": None}
        for n in names:  # any file in the pair can carry the capture time
            info = imgmeta.capture_info(os.path.join(FOLDER, n))
            if info["time"]:
                break
        exts = sorted({imgmeta.ext_of(n)[1:].upper() for n in names}, key=lambda e: e in ("JPG", "JPEG", "PNG"))
        shots.append({"id": sid, "name": names[0].rsplit("/", 1)[-1], "files": names, "label": "+".join(exts),
                      "time": info["time"] or "", "error": error, **meta})
    shots.sort(key=lambda s: (s["time"] or "9999", s["name"].lower()))
    return shots


def shot_info(sid):
    """Combined metadata and capture time of one shot (for exports)."""
    names = shot_files(sid)
    meta = {"caption": "", "creator": "", "rating": 0, "keywords": [], "people": []}
    for n in names:
        try:
            m = imgmeta.read_meta(os.path.join(FOLDER, n))
        except Exception:
            continue
        for k in ("caption", "creator"):
            meta[k] = meta[k] or m[k]
        meta["rating"] = meta["rating"] or m["rating"]
        for k in ("keywords", "people"):
            meta[k] = meta[k] + [x for x in m[k] if x.lower() not in {y.lower() for y in meta[k]}]
    when = None
    for n in names:
        when = imgmeta.capture_info(os.path.join(FOLDER, n))["time"]
        if when:
            break
    return {"files": names, "time": when, "meta": meta}


def export_available():
    return imgmeta.export_available()


# Lightroom: the photos are handed to it the way dropping them on its Dock icon does,
# so it opens its own import window with them picked. Ratings, captions and tags go along
# (inside JPEGs, and in the .xmp sidecars Lightroom reads next to RAW files).
LIGHTROOMS = [("com.adobe.LightroomClassicCC7", "Lightroom Classic"), ("com.adobe.lightroomCC", "Lightroom")]


# On Windows, Lightroom is started with the photos on its command line, which is what
# dropping files on its icon does there too.
WIN_LIGHTROOMS = {"com.adobe.LightroomClassicCC7": (r"Adobe Lightroom Classic", "Lightroom.exe"),
                  "com.adobe.lightroomCC": (r"Adobe Lightroom CC", "lightroom.exe")}
WIN_CMD_LIMIT = 30000  # Windows caps a command line at 32,767 characters


def _win_lightroom_exe(bid):
    folder, exe = WIN_LIGHTROOMS[bid]
    for base in {os.environ.get("ProgramW6432"), os.environ.get("ProgramFiles"), r"C:\Program Files"}:
        if base:
            p = os.path.join(base, "Adobe", folder, exe)
            if os.path.isfile(p):
                return p
    return None


def lightroom_apps():
    if os.name == "nt":
        return [{"id": bid, "name": name} for bid, name in LIGHTROOMS if _win_lightroom_exe(bid)]
    if sys.platform != "darwin":
        return []
    found = []
    for bid, name in LIGHTROOMS:
        try:
            r = subprocess.run(["mdfind", "kMDItemCFBundleIdentifier == '%s'" % bid], capture_output=True, text=True, timeout=5)
            if any(line.endswith(".app") for line in r.stdout.splitlines()):
                found.append({"id": bid, "name": name})
        except Exception:
            pass
    return found


def send_to_lightroom(bid, names):
    if bid not in {b for b, _ in LIGHTROOMS}:
        raise ValueError("Unknown Lightroom.")
    paths = [os.path.join(FOLDER, n) for n in names]
    if os.name == "nt":
        exe = _win_lightroom_exe(bid)
        if not exe:
            raise ValueError("Lightroom isn't installed in the usual place (Program Files\\Adobe).")
        cmd = subprocess.list2cmdline([exe] + paths)
        if len(cmd) > WIN_CMD_LIMIT:
            fit = max(1, int(len(names) * WIN_CMD_LIMIT / len(cmd)))
            raise ValueError("That's too many photos to hand to Lightroom at once on Windows. "
                             "Send about %d at a time." % fit)
        subprocess.Popen([exe] + paths, close_fds=True)
        return
    r = subprocess.run(["open", "-b", bid] + paths, capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise ValueError("Lightroom didn't open: " + (r.stderr.strip() or "unknown error"))


def shot_files(sid):
    """The files of one shot (relative to FOLDER), checked to still exist."""
    sid = str(sid).lower()
    if not FOLDER:
        return []
    if sid not in INDEX:
        INDEX.update(scan_folder())
    return [n for n in INDEX.get(sid, []) if os.path.isfile(os.path.join(FOLDER, n))]


def set_folder(path):
    global FOLDER, FOLDER_CARD
    FOLDER = path
    INDEX.clear()
    card = card_root(path)
    FOLDER_CARD = os.path.basename(card.rstrip("/\\")) or card if card else ""


def folder_info():
    return {"folder": FOLDER, "missing": bool(FOLDER) and not os.path.isdir(FOLDER),
            "on_card": bool(FOLDER_CARD), "card": FOLDER_CARD}


def friendly(e):
    if isinstance(e, PermissionError) and e.errno == 1 and sys.platform == "darwin":  # blocked by macOS privacy
        return ("macOS is blocking Caption Images from this folder. Open System Settings > Privacy & Security > "
                "Files and Folders, turn on Caption Images' switches, then quit and reopen Caption Images.")
    if isinstance(e, OSError) and e.errno in (13, 30):  # permission denied / read-only
        return ("Couldn't save: this card or folder is locked. If it's an SD card, slide the lock switch "
                "on its side up, then plug it back in.")
    if isinstance(e, FileNotFoundError):
        return "That photo isn't there any more. Was the card unplugged?"
    return str(e)


# -------------------------------------------------- code replacements ----

def _read_text(path):
    with open(path, "rb") as f:
        raw = f.read()
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            pass
    return raw.decode("latin-1")


def parse_codes(text):
    """Tab-delimited lines: code<TAB>replacement[<TAB>more columns...]."""
    codes = {}
    for line in text.splitlines():
        parts = line.split("\t")
        code = parts[0].strip()
        if code and len(parts) >= 2:
            codes[code] = [p.strip() for p in parts[1:]]
    return codes


def load_codes():
    files = []
    if os.path.isdir(CODES_DIR):
        for name in sorted(os.listdir(CODES_DIR), key=str.lower):
            if name.startswith(".") or os.path.splitext(name)[1].lower() not in CODE_EXTS:
                continue
            found = parse_codes(_read_text(os.path.join(CODES_DIR, name)))
            files.append({"name": name, "count": len(found), "codes": found})
    return {"files": files}


def load_templates():
    """{name: values} from the Metadata Templates folder."""
    out = {}
    if not os.path.isdir(TEMPLATES_DIR):
        return out
    for n in sorted(os.listdir(TEMPLATES_DIR)):
        if n.startswith(".") or not n.lower().endswith(".json"):
            continue
        try:
            d = json.loads(_read_text(os.path.join(TEMPLATES_DIR, n)))
        except (OSError, ValueError):
            continue
        if isinstance(d, dict) and isinstance(d.get("values"), dict):
            out[str(d.get("name") or n[:-5])] = {str(k): v for k, v in d["values"].items() if isinstance(v, str)}
    return out


def template_path(name):
    stem = ingest.safe_name(str(name))[:80]
    return os.path.join(TEMPLATES_DIR, stem + ".json") if stem else None


def save_template(name, values):
    p = template_path(name)
    if not p:
        raise ValueError("Type a name for the template.")
    os.makedirs(TEMPLATES_DIR, exist_ok=True)
    data = {"name": " ".join(str(name).split()), "values": {str(k): str(v) for k, v in (values or {}).items() if isinstance(v, (str, int, float))}}
    fd, tmp = tempfile.mkstemp(dir=TEMPLATES_DIR, prefix=".tpl-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.chmod(tmp, 0o644)
    os.replace(tmp, p)


def delete_template(name):
    for n in os.listdir(TEMPLATES_DIR) if os.path.isdir(TEMPLATES_DIR) else []:
        p = os.path.join(TEMPLATES_DIR, n)
        if n.lower().endswith(".json") and not n.startswith("."):
            try:
                d = json.loads(_read_text(p))
            except (OSError, ValueError):
                continue
            if isinstance(d, dict) and str(d.get("name") or n[:-5]) == name:
                os.remove(p)


def load_tags():
    if not os.path.isfile(TAGS_FILE):
        return []
    out, seen = [], set()
    for line in _read_text(TAGS_FILE).splitlines():
        t = line.strip()
        if t and not t.startswith("#") and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out


def new_code_file_path(name):
    name = os.path.basename(str(name)).strip()
    if not name or name.startswith("."):
        return None
    base, ext = os.path.splitext(name)
    if ext.lower() not in CODE_EXTS:
        base, ext = name, ".txt"
    path, n = os.path.join(CODES_DIR, base + ext), 2
    while os.path.exists(path):
        path, n = os.path.join(CODES_DIR, "%s (%d)%s" % (base, n, ext)), n + 1
    return path


# ------------------------------------------------------- jersey numbers ----
#
# jersey-numbers (built from jersey-numbers.m) reads the jersey numbers in a
# photo with Apple Vision, on this Mac. It finds each player, reads a zoomed-in
# crop of their jersey, sweeps the whole photo, and lets the reads vote.
# No face data is kept.
#
# On Windows there's no Apple Vision, so jersey_numbers_win.py reads numbers a
# different way: free, offline OCR (RapidOCR) over the whole photo plus a tile
# sweep, with the same voting idea. It doesn't find players by pose, so there's
# no torso crop and no "Teach numbers" training there (teach.available() stays
# Mac-only; see jersey_numbers_win.py's docstring for what's different).

NUMBERS_TOOL = os.path.join(APP_DIR, "jersey-numbers")
NUMBERS_CACHE = {}
NUMBERS_SLOTS = threading.Semaphore(2)  # reading photos is heavy; at most two at once


def numbers_available():
    if sys.platform == "darwin":
        return os.access(NUMBERS_TOOL, os.X_OK)
    return jersey_numbers_win.available()


def jersey_numbers(sid):
    """[{"n": "23", "alts": [...], "onPlayer": bool, "color": [r, g, b], ...}] for one photo (cached)."""
    names = shot_files(sid)
    if not names:
        raise FileNotFoundError("That photo is no longer in the folder.")
    key = (FOLDER, str(sid).lower())  # photos don't change here, only their metadata
    if key in NUMBERS_CACHE:
        return NUMBERS_CACHE[key]
    folder = FOLDER
    tmp = None
    with NUMBERS_SLOTS:
        try:
            tmp = photo_jpeg(sid)
            if sys.platform == "darwin":
                # each jersey is kept for a while, so tagging the player can teach the model ("Teach numbers")
                os.makedirs(teach.SEEN, exist_ok=True)
                args = [NUMBERS_TOOL, "--crops", teach.SEEN, "--name", teach.crop_name(folder, sid)] + teach.model_args()
                r = subprocess.run(args + [tmp], capture_output=True, timeout=180)
                try:
                    out = json.loads(r.stdout.decode("utf-8").strip().splitlines()[-1]).get("numbers", [])
                except (ValueError, IndexError):
                    out = []
            else:
                try:
                    out = jersey_numbers_win.read_numbers(tmp)
                except Exception:
                    out = []
        finally:
            if tmp:
                os.unlink(tmp)
    NUMBERS_CACHE[key] = out
    READS[0] += 1
    if READS[0] % 200 == 0:
        threading.Thread(target=teach.prune_seen, daemon=True).start()
    threading.Thread(target=imgmeta.prune_previews, daemon=True).start()
    return out


READS = [0]


def photo_jpeg(sid):
    """A temporary full-size JPEG of a photo for the number reader (the RAW's preview, or a copy). Caller deletes it."""
    names = shot_files(sid)
    if not names:
        raise FileNotFoundError("That photo is no longer in the folder.")
    file = os.path.join(FOLDER, names[0])
    data = imgmeta.preview(file, "full")
    if data:
        fd, tmp = tempfile.mkstemp(suffix=".jpg", prefix="caption-images-")
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        return tmp
    fd, tmp = tempfile.mkstemp(suffix=imgmeta.ext_of(file) or ".jpg", prefix="caption-images-")
    os.close(fd)
    shutil.copyfile(file, tmp)
    return tmp


# ------------------------------------------------------------- server ----

class Server(ThreadingHTTPServer):
    # HTTPServer turns this on by default. On macOS/Linux that only lets a port in TIME_WAIT be
    # reused; on Windows it also lets a second process bind the port while the first is still
    # listening - both end up serving 127.0.0.1:8765 with different TOKENs, and incoming
    # connections land on whichever one Windows feels like, so requests fail unpredictably
    # ("Forbidden" on some POSTs, not others). Turning it off restores the behaviour main()'s
    # "except OSError: use a random port instead" already assumes: a second launch should fail
    # to bind and fall back cleanly, the same way it already does on a Mac.
    allow_reuse_address = False

    def handle_error(self, request, client_address):
        """A page closing or reloading mid-request resets the connection; that's normal, not
        a bug, and logging it on every reload would bury a real error under routine noise."""
        if isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)):
            return
        super().handle_error(request, client_address)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8", cache=False):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "private, max-age=3600" if cache else "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the page moved on (e.g. skipped past a photo)

    def _host_ok(self):
        return self.headers.get("Host", "").split(":")[0] in ("127.0.0.1", "localhost")

    def do_GET(self):
        try:
            self._get()
        except Exception as e:
            self._send(500, {"error": friendly(e)})

    def _get(self):
        if not self._host_ok():
            return self._send(403, {"error": "Forbidden"})
        url = urllib.parse.urlparse(self.path)
        path, query = url.path, urllib.parse.parse_qs(url.query)
        if path == "/":
            with open(os.path.join(APP_DIR, "app.html"), encoding="utf-8") as f:
                return self._send(200, f.read().replace("__TOKEN__", TOKEN), "text/html; charset=utf-8")
        if path == "/api/shots":
            return self._send(200, dict(folder_info(), shots=folder_shots()))
        if path == "/api/ping":
            LAST_SEEN[0] = time.monotonic()
            return self._send(200, {"app": APP_ID, "v": APP_VERSION})
        if path == "/api/lightroom":
            return self._send(200, {"apps": lightroom_apps()})
        if path == "/api/templates":
            return self._send(200, {"templates": load_templates(), "folder": TEMPLATES_DIR})
        if path == "/api/tags":
            return self._send(200, {"tags": load_tags(), "file": TAGS_FILE})
        if path == "/api/codes":
            return self._send(200, dict(load_codes(), dir=CODES_DIR))
        if path == "/api/cards":
            return self._send(200, {"cards": ingest.find_cards()})
        if path == "/api/job":
            return self._send(200, JOB.state if JOB else {"running": False, "finished": False})
        if path == "/api/export/available":
            return self._send(200, {"available": export_available()})
        if path == "/api/players":
            if not numbers_available():
                return self._send(200, {"available": False, "numbers": []})
            return self._send(200, {"available": True, "numbers": jersey_numbers(query.get("id", [""])[0])})
        if path == "/api/teach/status":
            return self._send(200, dict(teach.status(), reader=numbers_available()))
        if path == "/api/teach/waiting":
            return self._send(200, {"jerseys": teach.waiting(query.get("n", ["30"])[0])})
        if path.startswith("/teach/crop/"):
            p = teach.crop_path(urllib.parse.unquote(path[12:]))
            if not p:
                return self._send(404, {"error": "Not found"})
            with open(p, "rb") as f:
                return self._send(200, f.read(), "image/jpeg", cache=True)
        if path.startswith("/photo/"):
            return self._photo(urllib.parse.unquote(path[7:]), query.get("size", ["medium"])[0])
        self._send(404, {"error": "Not found"})

    def _photo(self, sid, size):
        names = shot_files(sid)
        if not names:
            return self._send(404, {"error": "Not found"})
        file = os.path.join(FOLDER, names[0])
        try:
            data = imgmeta.preview(file, size if size in ("thumb", "medium", "screen", "full") else "medium")
        except Exception:
            data = None
        if data is None and imgmeta.ext_of(file) in imgmeta.EMBED_EXTS:
            with open(file, "rb") as f:
                return self._send(200, f.read(), mimetypes.guess_type(file)[0] or "image/jpeg", cache=True)
        if data is None:
            return self._send(404, {"error": "No preview for this file"})
        self._send(200, data, "image/jpeg", cache=True)

    def _teach(self, action, req):
        if not (numbers_available() and teach.available()):
            return self._send(400, {"error": "Teaching numbers needs a Mac."})
        try:
            if action == "scan":
                if not FOLDER:
                    return self._send(400, {"error": "Open a folder of photos first."})
                if not INDEX:
                    folder_shots()
                started = teach.scan(FOLDER, list(INDEX.keys()), photo_jpeg, NUMBERS_TOOL, NUMBERS_SLOTS)
                return self._send(200, dict(teach.status(), started=started))
            if action == "label":
                return self._send(200, teach.label(req.get("crop"), req.get("label")))
            if action == "undo":
                return self._send(200, teach.undo(req.get("crop"), req.get("label"), req.get("entry")))
            if action == "confirm":
                return self._send(200, teach.confirm(req.get("crop"), req.get("n")))
            if action == "train":
                teach.train(on_done=NUMBERS_CACHE.clear)
                return self._send(200, teach.status())
            if action == "forget":
                teach.forget_model()
                NUMBERS_CACHE.clear()
                return self._send(200, teach.status())
        except ValueError as e:
            return self._send(400, {"error": str(e)})
        return self._send(404, {"error": "Not found"})

    def do_POST(self):
        global FOLDER, JOB
        if not self._host_ok() or self.headers.get("X-Token") != TOKEN:
            return self._send(403, {"error": "Forbidden"})
        path = urllib.parse.urlparse(self.path).path
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length > 20 * 1024 * 1024:
                return self._send(413, {"error": "That file is too large."})
            req = json.loads(self.rfile.read(length) or b"{}")

            if path == "/api/quit":
                if JOB and JOB.state["running"] and not req.get("force"):
                    return self._send(409, {"error": "A copy is still running."})
                if JOB:
                    JOB.cancel.set()
                self._send(200, {"ok": True})
                print("Closing: quit requested from the page.", flush=True)
                threading.Thread(target=SERVER.shutdown, daemon=True).start()
                return
            if path == "/api/pick-folder":
                return self._send(200, {"path": pick_folder(req.get("prompt", "Choose a folder"), req.get("start"))})
            if path == "/api/folder":
                p = os.path.abspath(os.path.expanduser(str(req.get("path", ""))))
                if not os.path.isdir(p):
                    return self._send(400, {"error": "That folder doesn't exist."})
                set_folder(p)
                return self._send(200, folder_info())

            if path == "/api/meta":
                names = shot_files(req.get("id", ""))
                if not names:
                    return self._send(404, {"error": "That photo is no longer in the folder."})
                kw = {k: req[k] for k in ("caption", "creator", "rating", "keywords", "people", "tagged", "colorclass") if req.get(k) is not None}
                for k in ("keywords", "people"):
                    if k in kw:
                        kw[k] = [str(x) for x in kw[k]] if isinstance(kw[k], list) else []
                out = None
                with LOCK:
                    for n in names:
                        out = imgmeta.write_meta(os.path.join(FOLDER, n), **{k: (int(v) if k in ("rating", "colorclass") else bool(v) if k == "tagged" else v if k in ("keywords", "people") else str(v)) for k, v in kw.items()})
                return self._send(200, out)

            if path == "/api/ingest/scan":
                src = str(req.get("source", ""))
                if not os.path.isdir(src):
                    return self._send(400, {"error": "That folder or card isn't available. Is the card still plugged in?"})
                SCANS[src] = ingest.scan(src)
                return self._send(200, ingest.summary(src, SCANS[src]))
            if path == "/api/ingest/start":
                if JOB and JOB.state["running"]:
                    return self._send(409, {"error": "Another copy is still running."})
                src, base = str(req.get("source", "")), str(req.get("base", ""))
                if src not in SCANS:
                    return self._send(400, {"error": "Choose a card or folder to copy from first."})
                if not os.path.isdir(base):
                    return self._send(400, {"error": "Choose where the photos should go."})
                shots = SCANS[src]
                when = shots[0]["time"] if shots else None
                sub = ingest.render(str(req.get("folder_template", "")), when, str(req.get("event", "")), 1, "", "", False) if when else ""
                dest = os.path.join(base, sub) if sub else base
                if os.path.abspath(dest).startswith(os.path.abspath(src) + os.sep) or os.path.abspath(dest) == os.path.abspath(src):
                    return self._send(400, {"error": "The destination can't be on the card itself."})
                opts = {"dest": dest, "rename": bool(req.get("rename")), "template": str(req.get("template", "{date}_{num}")),
                        "event": str(req.get("event", "")), "photographer": str(req.get("photographer", "")),
                        "skip_done": bool(req.get("skip_done", True)), "start_num": req.get("start_num", 1),
                        "only": str(req.get("only", "all")),
                        "meta": req.get("meta") if isinstance(req.get("meta"), dict) else {}}
                JOB = ingest.Job("ingest").start(ingest.run_ingest, shots, opts)
                JOB.state["dest"] = dest
                return self._send(200, JOB.state)
            if path == "/api/job/cancel":
                if JOB:
                    JOB.cancel.set()
                return self._send(200, JOB.state if JOB else {})

            if path == "/api/cull/copy":
                if JOB and JOB.state["running"]:
                    return self._send(409, {"error": "Another copy is still running."})
                dest = str(req.get("dest", ""))
                if not os.path.isdir(dest):
                    return self._send(400, {"error": "Choose a folder to copy to."})
                if os.path.abspath(dest) == os.path.abspath(FOLDER):
                    return self._send(400, {"error": "Choose a different folder than the one you're culling."})
                move = bool(req.get("move"))
                if move and FOLDER_CARD:
                    return self._send(400, {"error": "Photos can't be moved off a memory card here. Copy them instead; the card stays as it is."})
                names = [n for sid in req.get("ids", []) for n in shot_files(sid)]
                JOB = ingest.Job("copy").start(ingest.run_copy, FOLDER, names, dest, move, LOCK)
                return self._send(200, JOB.state)
            if path == "/api/export":
                if not export_available():
                    return self._send(400, {"error": "Smaller copies need a Mac."})
                if JOB and JOB.state["running"]:
                    return self._send(409, {"error": "Another copy is still running."})
                dest = str(req.get("dest", ""))
                if not os.path.isdir(dest):
                    return self._send(400, {"error": "Choose a folder for the smaller copies."})
                if os.path.abspath(dest) == os.path.abspath(FOLDER):
                    return self._send(400, {"error": "Choose a different folder, so the copies don't mix with the originals."})
                shots = [shot_info(sid) for sid in req.get("ids", [])]
                shots = [x for x in shots if x["files"]]
                opts = {k: req.get(k) for k in ("long_edge", "quality", "max_mb")}
                JOB = ingest.Job("copy").start(ingest.run_export, FOLDER, shots, dest, opts)
                return self._send(200, JOB.state)
            if path == "/api/lightroom":
                names = [n for sid in req.get("ids", []) for n in shot_files(sid)]
                if not names:
                    return self._send(400, {"error": "No photos to send."})
                try:
                    send_to_lightroom(str(req.get("app", "")), names)
                except ValueError as e:
                    return self._send(400, {"error": str(e)})
                return self._send(200, {"sent": len(names)})
            if path == "/api/cull/move-rejects":
                if FOLDER_CARD:
                    return self._send(400, {"error": "Rejects stay put on a memory card. Leave them out when you ingest instead."})
                names = [n for sid in req.get("ids", []) for n in shot_files(sid)]
                with LOCK:
                    dest, moved = ingest.move_to_subfolder(FOLDER, names)
                return self._send(200, {"dest": dest, "moved": moved})

            if path.startswith("/api/teach/"):
                return self._teach(path[11:], req)

            if path == "/api/templates/save":
                save_template(str(req.get("name", "")).strip(), req.get("values") or {})
                return self._send(200, {"templates": load_templates()})
            if path == "/api/templates/delete":
                delete_template(str(req.get("name", "")))
                return self._send(200, {"templates": load_templates()})
            if path == "/api/codes/add":
                text = str(req.get("text", ""))
                if not parse_codes(text):
                    return self._send(400, {"error": "No codes found. Each line should be a code, a tab, then the replacement text."})
                os.makedirs(CODES_DIR, exist_ok=True)
                dest = new_code_file_path(req.get("name", ""))
                if not dest:
                    return self._send(400, {"error": "That file name can't be used."})
                with open(dest, "w", encoding="utf-8") as f:
                    f.write(text)
                d = load_codes()
                d["added"] = os.path.basename(dest)
                return self._send(200, d)
            self._send(404, {"error": "Not found"})
        except Exception as e:
            self._send(500, {"error": friendly(e)})


def already_running():
    """True if Caption Images is already serving on our port."""
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/api/ping" % PORT, timeout=1.5) as r:
            return json.load(r).get("app") == APP_ID
    except Exception:
        return False


def open_window(url):
    """Open the app in its own window (Chrome/Edge/Brave app mode) if possible, else a browser tab."""
    if sys.platform == "darwin":
        for name in ("Google Chrome", "Microsoft Edge", "Brave Browser", "Chromium"):
            for root in ("/Applications", os.path.expanduser("~/Applications")):
                if os.path.isdir(os.path.join(root, name + ".app")):
                    if subprocess.run(["open", "-na", os.path.join(root, name + ".app"), "--args", "--app=" + url]).returncode == 0:
                        return
    elif os.name == "nt":
        pf = [os.environ.get(k, "") for k in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA")]
        for rel in (r"Microsoft\Edge\Application\msedge.exe", r"Google\Chrome\Application\chrome.exe"):
            for base in pf:
                exe = os.path.join(base, rel)
                if base and os.path.isfile(exe):
                    subprocess.Popen([exe, "--app=" + url])
                    return
    webbrowser.open(url)


def watch_idle():
    """Close the app a few minutes after its window is closed, unless a copy is running."""
    while True:
        time.sleep(15)
        idle_for = time.monotonic() - LAST_SEEN[0]
        if idle_for > IDLE_QUIT and not (JOB and JOB.state["running"]):
            print("Closing: no page has checked in for %d seconds (limit %d)." % (idle_for, IDLE_QUIT), flush=True)
            SERVER.shutdown()
            return


def tell_user(message):
    """Show an error even when there's no Terminal window (double-clicked app)."""
    print(message, file=sys.stderr, flush=True)
    if sys.platform == "darwin":
        msg = message.replace("\\", "/").replace('"', "'")
        subprocess.run(["osascript", "-e", 'display alert "Caption Images" message "%s"' % msg], capture_output=True)
    elif os.name == "nt":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, message, "Caption Images", 0x10)
        except Exception:
            pass


def main():
    global SERVER
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    folder = ""
    if args:
        folder = os.path.abspath(os.path.expanduser(args[0]))
        if not os.path.isdir(folder):
            sys.exit("Not a folder: " + folder)
    show = "--no-browser" not in sys.argv
    if already_running():  # opened twice: just bring up a window on the running copy
        if show:
            open_window("http://127.0.0.1:%d/" % PORT)
        return
    if folder:
        set_folder(folder)
    threading.Thread(target=teach.prune_seen, daemon=True).start()
    threading.Thread(target=imgmeta.prune_previews, daemon=True).start()
    # A fixed port keeps the browser's remembered settings between runs.
    try:
        SERVER = Server(("127.0.0.1", PORT), Handler)
    except OSError:  # something else is using it
        SERVER = Server(("127.0.0.1", 0), Handler)
    url = "http://127.0.0.1:%d/" % SERVER.server_address[1]
    print("Caption Images is running at", url, flush=True)
    if show:
        open_window(url + ("#cull" if FOLDER else "#ingest"))
    if "--stay" not in sys.argv:
        threading.Thread(target=watch_idle, daemon=True).start()
    try:
        SERVER.serve_forever()
    except KeyboardInterrupt:
        pass
    print("Caption Images closed.", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        tell_user("Caption Images couldn't start: %s" % e)
        raise
