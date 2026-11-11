"""Teach numbers: you label jerseys, the app learns to read your teams' numbers.

Everything stays on this Mac, in ~/Library/Application Support/Caption Images/Numbers:
  labels/<number>/   jerseys you labelled (and players you tagged from a read)
  inbox/             jerseys waiting for a label
  skipped/           jerseys you couldn't make out (kept so Undo works)
  seen/              jerseys from photos the app has read (kept for a while, so
                     tagging a player can label their jersey)
  model/             the trained model (numbers.mlmodelc) and info.json
Only cropped jerseys are kept, never whole photos or faces.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

APP_DIR = os.path.dirname(os.path.abspath(__file__))
TRAIN_TOOL = os.path.join(APP_DIR, "jersey-train")
BASE = os.environ.get("CAPTION_IMAGES_NUMBERS") or os.path.expanduser("~/Library/Application Support/Caption Images/Numbers")
LABELS = os.path.join(BASE, "labels")
INBOX = os.path.join(BASE, "inbox")
SEEN = os.path.join(BASE, "seen")
SKIPPED = os.path.join(BASE, "skipped")
MODEL_DIR = os.path.join(BASE, "model")
MODEL = os.path.join(MODEL_DIR, "numbers.mlmodelc")
INFO = os.path.join(MODEL_DIR, "info.json")
INBOX_INDEX = os.path.join(BASE, "inbox.json")
SCANNED = os.path.join(BASE, "scanned.json")

LABEL_RE = re.compile(r"^(?:\d{1,2}|none)$")
CROP_RE = re.compile(r"^[A-Za-z0-9_.-]{1,120}\.jpg$")
SCAN_BATCH = 40  # photos looked through per "Find jerseys"
SEEN_KEEP = 3000
MIN_PER = 3  # the trainer skips numbers with fewer labels

LOCK = threading.Lock()
STATE = {"scanning": False, "scanned": 0, "to_scan": 0, "training": False, "error": ""}


def available():
    return sys.platform == "darwin" and os.access(TRAIN_TOOL, os.X_OK)


def model_args():
    """Extra arguments for the number reader: use the trained model if there is one."""
    return ["--model", MODEL] if os.path.isdir(MODEL) else []


def crop_name(folder, sid):
    """A file-name-safe, unique stem for one photo's jerseys."""
    h = hashlib.sha1((folder + "\0" + str(sid).lower()).encode("utf-8")).hexdigest()[:10]
    safe = re.sub(r"[^A-Za-z0-9]+", "-", str(sid))[:40].strip("-")
    return h + "_" + (safe or "photo")


def _load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _save(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def _check_crop(name):
    if not CROP_RE.match(str(name or "")) or ".." in name:
        raise ValueError("Unknown jersey.")
    return name


def _check_label(label):
    label = str(label).strip().lower().lstrip("#")
    if label.isdigit():
        label = str(int(label))
    if not LABEL_RE.match(label) or (label.isdigit() and int(label) > 99):
        raise ValueError("Type a number from 0 to 99.")
    return label


def counts():
    out = {}
    try:
        for label in os.listdir(LABELS):
            d = os.path.join(LABELS, label)
            if LABEL_RE.match(label) and os.path.isdir(d):
                n = sum(1 for f in os.listdir(d) if f.endswith(".jpg"))
                if n:
                    out[label] = n
    except OSError:
        pass
    return out


def _labelled_as(name):
    for label in counts():
        if os.path.exists(os.path.join(LABELS, label, name)):
            return label
    return None


def status():
    with LOCK:
        inbox = len(_load(INBOX_INDEX, []))
        st = dict(STATE)
    info = _load(INFO, None) if os.path.isdir(MODEL) else None
    return dict(st, available=available(), counts=counts(), waiting=inbox, model=info, min_per=MIN_PER)


def crop_path(name):
    """Where a jersey crop is: waiting, seen, or labelled."""
    name = _check_crop(name)
    for d in (INBOX, SEEN, SKIPPED):
        p = os.path.join(d, name)
        if os.path.isfile(p):
            return p
    label = _labelled_as(name)
    return os.path.join(LABELS, label, name) if label else None


def waiting(n=30):
    with LOCK:
        return _load(INBOX_INDEX, [])[: max(1, min(int(n), 200))]


def label(name, value):
    """Label a waiting jersey: a number, "none" (no number showing), or "skip" (can't tell)."""
    name = _check_crop(name)
    with LOCK:
        idx = _load(INBOX_INDEX, [])
        entry = next((e for e in idx if e["crop"] == name), None)
        idx = [e for e in idx if e["crop"] != name]
        src = os.path.join(INBOX, name)
        if value == "skip":
            if os.path.exists(src):
                os.makedirs(SKIPPED, exist_ok=True)
                shutil.move(src, os.path.join(SKIPPED, name))
        else:
            value = _check_label(value)
            if not os.path.exists(src):
                raise ValueError("That jersey is gone.")
            dest_dir = os.path.join(LABELS, value)
            os.makedirs(dest_dir, exist_ok=True)
            shutil.move(src, os.path.join(dest_dir, name))
        _save(INBOX_INDEX, idx)
    return {"ok": True, "entry": entry}


def undo(name, value, entry=None):
    """Put a jersey back in the inbox (the last label was a mistake)."""
    name = _check_crop(name)
    with LOCK:
        src = os.path.join(SKIPPED, name) if value == "skip" else os.path.join(LABELS, _check_label(value), name)
        if os.path.exists(src):
            os.makedirs(INBOX, exist_ok=True)
            shutil.move(src, os.path.join(INBOX, name))
        if not os.path.exists(os.path.join(INBOX, name)):
            raise ValueError("That jersey can't be brought back.")
        idx = [e for e in _load(INBOX_INDEX, []) if e["crop"] != name]
        e = entry if isinstance(entry, dict) and entry.get("crop") == name else {"crop": name}
        _save(INBOX_INDEX, [e] + idx)
    return {"ok": True}


def confirm(name, number):
    """You tagged a player from a read: their jersey is a labelled example."""
    name = _check_crop(name)
    number = _check_label(number)
    src = os.path.join(SEEN, name)
    with LOCK:
        if not os.path.isfile(src) or _labelled_as(name):
            return {"ok": False}
        dest_dir = os.path.join(LABELS, number)
        os.makedirs(dest_dir, exist_ok=True)
        shutil.copyfile(src, os.path.join(dest_dir, name))
    return {"ok": True}


def prune_seen(keep=SEEN_KEEP):
    _prune(SKIPPED, 500)
    _prune(SEEN, keep)


def _prune(folder, keep):
    try:
        files = [os.path.join(folder, f) for f in os.listdir(folder) if f.endswith(".jpg")]
    except OSError:
        return
    if len(files) <= keep:
        return
    files.sort(key=lambda p: os.path.getmtime(p))
    for p in files[: len(files) - keep]:
        try:
            os.unlink(p)
        except OSError:
            pass


# ---- finding jerseys in a folder ----

def scan(folder, sids, photo_jpeg, reader, slots):
    """Look through up to SCAN_BATCH photos of `folder` (spread across it) for jerseys to label.
    photo_jpeg(sid) -> a temporary JPEG path (caller's helper) or None; reader is the jersey-numbers tool."""
    with LOCK:
        if STATE["scanning"]:
            return False
        done = set(_load(SCANNED, []))
        todo = [s for s in sids if crop_name(folder, s) not in done]
        if len(todo) > SCAN_BATCH:  # spread out, so one burst doesn't fill the inbox with the same jersey
            step = len(todo) / SCAN_BATCH
            todo = [todo[int(i * step)] for i in range(SCAN_BATCH)]
        STATE.update(scanning=True, scanned=0, to_scan=len(todo), error="")
    threading.Thread(target=_scan, args=(folder, todo, photo_jpeg, reader, slots), daemon=True).start()
    return True


def _scan(folder, todo, photo_jpeg, reader, slots):
    os.makedirs(INBOX, exist_ok=True)
    queue = list(todo)
    qlock = threading.Lock()

    def work():
        while True:
            with qlock:
                if not queue:
                    return
                sid = queue.pop(0)
            stem = crop_name(folder, sid)
            tmp = None
            try:
                with slots:
                    tmp = photo_jpeg(sid)
                    if not tmp:
                        continue
                    r = subprocess.run([reader, "--fast", "--crops", INBOX, "--name", stem] + model_args() + [tmp],
                                       capture_output=True, timeout=180)
                lines = r.stdout.decode("utf-8").strip().splitlines()
                jerseys = json.loads(lines[-1]).get("jerseys", []) if lines else []
                new = []
                for j in jerseys:
                    if not j.get("crop"):
                        continue
                    e = {"crop": j["crop"], "photo": str(sid)}
                    if j.get("read"):
                        e.update(guess=j["read"], by="read")
                    elif j.get("model") and j.get("conf", 0) >= 0.5:
                        e.update(guess=j["model"], by="model", conf=j["conf"])
                    new.append(e)
                with LOCK:
                    idx = _load(INBOX_INDEX, [])
                    have = {e["crop"] for e in idx}
                    idx += [e for e in new if e["crop"] not in have and not _labelled_as(e["crop"])]
                    _save(INBOX_INDEX, idx)
                    scanned = _load(SCANNED, [])
                    scanned.append(stem)
                    _save(SCANNED, scanned[-20000:])
            except Exception as e:  # one bad photo shouldn't stop the rest
                STATE["error"] = str(e)
            finally:
                if tmp:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
                with LOCK:
                    STATE["scanned"] += 1

    threads = [threading.Thread(target=work, daemon=True) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    with LOCK:
        STATE["scanning"] = False


# ---- training ----

def train(on_done=None):
    with LOCK:
        if STATE["training"]:
            return False
        STATE.update(training=True, error="")
    threading.Thread(target=_train, args=(on_done,), daemon=True).start()
    return True


def _train(on_done):
    try:
        r = subprocess.run([TRAIN_TOOL, LABELS, MODEL_DIR], capture_output=True, timeout=1800)
        lines = r.stdout.decode("utf-8", "replace").strip().splitlines()
        out = json.loads(lines[-1]) if lines else {"ok": False, "error": "The trainer stopped unexpectedly."}
        if out.get("ok"):
            out["when"] = time.time()
            out["labels"] = sum(out.get("numbers", {}).values())
            _save(INFO, out)
            if on_done:
                on_done()
        else:
            STATE["error"] = out.get("error") or "Training didn't work."
    except Exception as e:
        STATE["error"] = "Training didn't work: %s" % e
    finally:
        with LOCK:
            STATE["training"] = False


def forget_model():
    """Delete the trained model (labels are kept)."""
    with LOCK:
        shutil.rmtree(MODEL_DIR, ignore_errors=True)
