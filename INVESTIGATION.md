# Caption Images: Windows crash investigation & cross-platform architecture review

Investigated read-only; no code was changed. This is reasoned from the source in this repo
(commit `eb4f35f`, "README: note the Windows crash report"). **No Windows machine was available
in this session, and no Windows stack trace or log exists in the repo** — the only evidence of
the crash is the one-line README note ("The Windows version opens and then crashes"). Everything
under "Windows crash" below is therefore ranked by confidence and clearly labeled where it's a
hypothesis rather than a confirmed bug. The fastest way to turn hypotheses into certainty is
described at the end.

## Architecture overview

Caption Images is **not** an AI image-captioning model. It's a Photo Mechanic replacement for a
college sports photo desk: ingest photos off a card → cull (star/reject) → write captions and
IPTC/XMP metadata → optionally hand off to Lightroom. One extra feature, "Teach numbers," uses
on-device AI (Apple Vision OCR + a small trained CoreML classifier) to read jersey numbers so the
UI can suggest player names.

```
caption.py (HTTP server, stdlib only)
  ├── imgmeta.py   — reads/writes XMP/IPTC/EXIF (JPEG/PNG) and .xmp sidecars (RAW/HEIC/TIFF); previews
  ├── ingest.py    — card detection, copy/rename, metadata templates, Getty captions, copy/move/export jobs
  ├── teach.py     — jersey-number labelling & training workflow (data + model paths; Mac-only tool invocation)
  ├── jersey-numbers  (compiled Objective-C binary, Apple Vision + CoreML)  — reads jersey numbers from a photo
  └── jersey-train    (compiled Swift binary, CreateML/CoreML)             — trains the personal number model
app.html — the entire UI (HTML/CSS/JS, ~2,700 lines), served by caption.py and polling its JSON API
```

End-to-end "caption" flow: `app.html` JS calls `/api/shots` → `caption.py` walks the working folder
(`scan_folder`) → `imgmeta.read_meta()` parses existing XMP/IPTC/EXIF per file → the page shows
previews (`imgmeta.preview()`, rendered via macOS `sips` or Pillow) and an editable caption → the
user types/edits → `POST /api/meta` → `imgmeta.write_meta()` rewrites the XMP/IPTC/EXIF segments (JPEG/PNG)
or the `.xmp` sidecar (RAW/HEIC/TIFF) in place. **No OCR or AI model is in this path at all.**

The only AI/vision step is optional and separate: `GET /api/players` → `jersey_numbers()` shells out
to the `jersey-numbers` binary on a temp JPEG, which uses Apple Vision (pose detection, text
recognition) and, if a personal model has been trained, a CoreML classifier, and prints JSON. This
only ever runs if `numbers_available()` is true.

Dependencies: Python 3 standard library only, for the server and all metadata/XMP/IPTC/EXIF/PNG
parsing (`imgmeta.py`, `ingest.py` have **zero** third-party imports). Two optional dependencies —
Pillow and pillow-heif — are pip-installed on first run on Windows (`Caption Images (Windows).bat`,
lines 14–20) and are used only for image previews/resizing and HEIC support; the app degrades (no
smaller-copy export, no HEIC preview) if they fail to install, but still starts. No requirements.txt
or pyproject.toml exists; there's nothing to "install" beyond those two optional packages.

## Resolving the Apple-AI-vs-Windows contradiction

The earlier Claude Code session was right about one feature and wrong to generalize it to the
whole app. Verified in code:

- `jersey-numbers` and `jersey-train` are **compiled Mach-O binaries** (confirmed with `file`:
  universal x86_64/arm64 Mach-O executables), built from `jersey-numbers.m` (imports
  `<CoreML/CoreML.h>`, `<Vision/Vision.h>`; uses `VNCoreMLRequest`,
  `VNRequestTextRecognitionLevelAccurate`) and `jersey-train.swift` (`import CreateML`,
  `import CoreML`). These literally cannot run on Windows — there's no Windows build, and the
  binary format itself isn't executable there.
- Every call site that touches these binaries or other Mac-only tools is gated behind a platform
  check that **disables the feature** on Windows rather than trying to run it:
  - `caption.py:437` `numbers_available()` → `sys.platform == "darwin" and os.access(NUMBERS_TOOL, os.X_OK)`
  - `teach.py:47` `available()` → same pattern, gates the whole "Teach numbers" workflow
  - `imgmeta.py:1068` `USE_SIPS = sys.platform == "darwin" and os.path.exists("/usr/bin/sips")` — previews/exports fall back to Pillow when false
  - `caption.py:103` `pick_folder()` — AppleScript `osascript` on macOS, `tkinter` file dialog on everything else
  - `caption.py:48` `_original_path()` — macOS "App Translocation" workaround, no-ops elsewhere
  - `caption.py:246/265` `lightroom_apps()`/`send_to_lightroom()` — `os.name == "nt"` branch looks in `Program Files`, darwin branch uses `mdfind`/`open -b`
  - `caption.py:771/801` `open_window()`/`tell_user()` — separate darwin/`nt` branches

So on Windows the app isn't running Apple Vision/CoreML at all — it runs with that one feature
(and the macOS `sips`-based preview path) switched off, falling back to Pillow. This also means:
**"Teach numbers" and jersey-number reading in the Caption tab simply won't appear/work on
Windows by design**, which is worth telling Coyle explicitly — it may be contributing to the
"something's wrong on Windows" impression even though it isn't the crash.

## Platform-specific code inventory

| File:Line | What it does | Guarded? | Windows fallback |
|---|---|---|---|
| `caption.py:48` `_original_path()` | Resolves macOS "App Translocation" temp path | Yes, `sys.platform != "darwin"` returns input unchanged | No-op (not needed on Windows) |
| `caption.py:103` `pick_folder()` | AppleScript folder picker (`osascript`) | Yes, `if sys.platform == "darwin"` else branch | `tkinter.filedialog.askdirectory`; if `tkinter` import fails, returns `None` (caught by broad `except Exception`) |
| `caption.py:246` `lightroom_apps()` | Finds installed Lightroom | Yes, `os.name == "nt"` / darwin / else | Looks in `Program Files`/`Program Files (x86)` for `Lightroom.exe`/`lightroom.exe` |
| `caption.py:265` `send_to_lightroom()` | Hands photos to Lightroom | Yes | `subprocess.Popen([exe] + paths)`; raises a friendly `ValueError` if not found or command line > 30,000 chars |
| `caption.py:437` `numbers_available()` | Gate for jersey-number OCR | Yes, `sys.platform == "darwin"` | Always `False`; `/api/players` returns `{"available": false, "numbers": []}` |
| `caption.py:771` `open_window()` | Opens the UI in an app-mode browser window | Yes, darwin / `os.name == "nt"` / else | Looks for Edge then Chrome in `Program Files`/`LOCALAPPDATA`; else `webbrowser.open()` |
| `caption.py:801` `tell_user()` | Shows a startup error with no console | Yes | `ctypes.windll.user32.MessageBoxW`, wrapped in `try/except Exception: pass` |
| `teach.py:25` `BASE` path constant | Where jersey labels/model are stored | Not platform-gated (just a path string) | Resolves to a nonsensical `...\Library/Application Support/...` path under the Windows user profile; harmless because `available()` (below) always gates actual use |
| `teach.py:47` `available()` | Gate for "Teach numbers" | Yes, `sys.platform == "darwin"` | Always `False`; `caption.py:585` returns `{"error": "Teaching numbers needs a Mac."}` |
| `imgmeta.py:1068` `USE_SIPS` | Use macOS `sips` for previews/resizing | Yes, `sys.platform == "darwin" and os.path.exists(...)` | Falls back to Pillow (`_pillow_preview`, `_make_smaller_pillow`) if installed; `preview()`/`export_available()` degrade gracefully if Pillow is missing |
| `imgmeta.py:1306` `_image_size()` | Calls `sips -g pixelWidth...` | **Not platform-guarded itself** — only called from `make_smaller()` which checks `USE_SIPS` first | Never reached on Windows (dead code path there) |
| `ingest.py:26` `find_cards()` | Finds memory cards | Yes, darwin / `os.name == "nt"` / else | Scans drive letters `D:` through `Z:` (`string.ascii_uppercase[3:]`) for a `DCIM` folder |
| `ingest.py:18` `SKIP_DIRS` | Folders to ignore while scanning | N/A (just includes both OS junk-folder names) | Includes `$RECYCLE.BIN`, `System Volume Information` |
| `Caption Images (Windows).bat` | Launches the app on Windows | N/A — this *is* the Windows entry point | Finds `py`/`python`, pip-installs Pillow/pillow-heif if missing, launches with `pyw`/`pythonw` |

No `platform.system()`, `coremltools`, `MLX`, `mps`/`cuda` device selection, or GPU code exists
anywhere in the Python — there is no ML framework dependency in the Python layer at all. All of
that lives exclusively in the two compiled Mac binaries.

## Windows crash: investigation and ranked hypotheses

**Update — Coyle got more detail from whoever hit this: it didn't open at all. "The moment they
would try to open the program it would crash, like they couldn't even do it."** That's a much
better match for the architectural problem below than for the port-reuse bug further down (which
needs the app to have started serving requests first). **Note throughout: Coyle develops on a
MacBook and can't run the Windows build himself** — the original report, and any follow-up, comes
from whoever on the team actually used it on Windows; he can relay questions but can't answer them
from firsthand experience, and neither can this testing session's own Windows laptop, which already
had a real Python installed for development before any of this testing started.

### Root-cause theory, now fixed: the app used to require a Python the machine probably doesn't have

**This is the leading explanation for "couldn't even open it,"** and it's structural, not a single
bad line: `Caption Images (Windows).bat` used to run `caption.py` with whatever `py`/`python`/
`pyw`/`pythonw` it could find on the machine's `PATH`. That's a reasonable assumption for a
developer's machine (this testing session's laptop included — real Python was already installed
for other work, which is exactly why this was never caught here until asked to think about *who*
actually reported the crash). **It's a bad assumption for a non-technical teammate's Windows 11
machine that's never had Python installed**, because Windows 11 ships `python.exe`/`python3.exe`
as built-in **App Execution Alias stubs**: typing `python` (or running it from a script) on such a
machine doesn't error — it silently opens the Microsoft Store to the Python listing page (or, when
invoked with arguments the way `caption.py` needs, just prints a "get Python from the Store"
message and exits) and does nothing useful. `where python` still finds this stub and reports
success, so the `.bat`'s Python-detection logic would think it found a working Python, then every
subsequent step (the one-time Pillow/RapidOCR install, then the actual launch) would silently fail
against the stub — no real error, no window, nothing. That lines up with "the moment they'd try to
open the program it would crash" far better than anything else investigated.

**Fix:** the app now ships its own Python (`Contents/Resources/python-windows/`, a Windows
"embeddable" CPython 3.12 build with `pip` bootstrapped in, ~29 MB, no packages pre-installed) and
`Caption Images (Windows).bat` uses it exclusively by its exact bundled path — it no longer calls
`where py`/`where python`/`where pyw`/`where pythonw` at all, so what is or isn't on the machine's
`PATH` can no longer matter. Verified on this laptop in three ways: (1) directly invoking the
bundled interpreter standalone — imports the app's own modules, installs Pillow/pillow-heif/
RapidOCR into its own isolated `Lib/site-packages` with no `--user` flag needed, and runs
`RapidOCR()` successfully; (2) running the real `.bat` end-to-end from a byte-for-byte fresh copy
of the app folder (not just this session's already-set-up one), confirming the process that ends
up listening on port 8765 is genuinely `...\python-windows\pythonw.exe`, not any system Python;
(3) a full regression pass through the real HTTP API (folder browsing, metadata, jersey-number
reading) against that bundled interpreter. **Can't reproduce the exact Store-stub failure itself on
this machine** (it already has real Python installed, which is the whole point), so this is a
strong, well-reasoned theory backed by a verified fix — not a confirmed root cause the way the
port-reuse bug below is. The actual confirmation can only come from someone else's Windows machine.

### Confirmed bug: two launches can end up serving the same port with different tokens

**Reproduced twice, directly, on real hardware.** `http.server.HTTPServer` (which `caption.py`'s
server is built on) sets `allow_reuse_address = True` by default. On macOS/Linux, `SO_REUSEADDR`
only helps reuse a port stuck in `TIME_WAIT` — it does **not** let two processes simultaneously
bind and listen on the same active port. **On Windows, `SO_REUSEADDR` is far more permissive: it
lets a second process bind to a port a first process is already listening on.** `main()`'s own
comment assumes the opposite (`except OSError: # something else is using it`, falling back to a
random port) — that fallback simply never triggers on Windows, because the second bind doesn't
fail there the way it does on a Mac.

Concretely: launch the app twice in close succession (an impatient double-click, or opening it
again before the first copy has fully started — easiest to hit during the ~28 second first-run
dependency install, timed on this laptop) and **both processes can end up listening on
127.0.0.1:8765 at once, each with its own independently-generated `TOKEN`.** Windows then routes
incoming connections between the two somewhat arbitrarily. `GET`s (page loads, `/api/ping`) don't
need the token and work regardless of which process answers; every `POST` (saving a caption,
rating, ingest, quit — anything that changes something) does need it, so whether a given save
succeeds or gets a `403 Forbidden` becomes a coin flip depending on which process's socket
happened to take that particular connection. That's an exact match for "it opens, then starts
behaving unpredictably" from the user's side, with no single error to point at.

**Fix:** set `allow_reuse_address = False` on the server (a 1-line change, `Server` subclass in
`caption.py`), which restores the behaviour the code already assumed it had — a second bind now
correctly fails, and the existing fallback-to-a-random-port logic (which already worked, untested,
on Windows) actually runs. Re-tested after the fix, under the same conditions (including the wide
~28s race window during a cold first-run install): only one process ever binds 8765 now; a second,
near-simultaneous launch correctly falls back to its own random port instead of colliding. What's
left after the fix is a much more benign cosmetic issue — two separate app windows, each pointed
at a different, independent server — not silent, intermittent data loss.

### Other hypotheses (unconfirmed, ranked by how likely each is to produce "opens, then crashes")

No Windows crash log or traceback exists for the original report, and the server and its
background threads are otherwise unusually well-guarded (broad `try/except` in every
`do_GET`/`do_POST` handler, `friendly()` error translation, `main()` wrapped in `try/except` with
a `MessageBoxW` fallback — see "What's been fixed so far" below for what's since been hardened).

### 1. (Superseded — the whole mechanism this describes no longer exists) The batch file can silently fail to find a working Python, even when Python is installed

**This entire hypothesis is now moot**: it was about the old `.bat`'s `where py`/`where python`/
`where pyw`/`where pythonw` detection logic failing in various ways. That detection logic doesn't
exist anymore — the `.bat` now always uses the bundled `python-windows/` interpreter by its exact
path (see "Root-cause theory, now fixed" above), so there's nothing left to fail to find. Left
below for the record of what was investigated, not as a live concern.

`Caption Images (Windows).bat:7-12` checks `where py` then `where python` to set `%PY%` (used only
for the one-time Pillow install check), but the actual app launch at lines 22-31 requires **a
different pair of executables**, `pyw` or `pythonw`, to exist on `PATH`. On a fresh Windows
machine it's common to have:
- Python installed from the Microsoft Store, which does **not** register `py`/`pyw` and sometimes
  not `pythonw` either (only `python.exe`/`python3.exe`), or
- Python installed from python.org **without** ticking "Install launcher for all users" — `pyw.exe`
  not registered, `pythonw.exe` present but not always on `PATH`.

If neither `pyw` nor `pythonw` resolves, the script falls straight to `:nopython` (line 33) and
prints "Caption Images needs Python... install it..." — this is a clean failure, not a crash, but
if the Windows user saw a console flash and vanish (a `.bat` window closes instantly unless
something pauses it) it is easy to describe after the fact as "it opens and then crashes." **This
doesn't match "it opens" meaning the browser UI appeared**, so treat this as the leading hypothesis
only if the app's web page never actually showed up — worth asking directly (see Open Questions;
route through Coyle to whoever actually ran it, since he's on a Mac and can't test this himself).

**Minimal fix:** add a `pause` (or an explicit message box via `ctypes`/`msg`) before exiting on
every failure branch, and fall back to `%PY% -m pythonw` / `%PY% -m pyw` style invocation using the
already-detected `%PY%` instead of requiring separately-registered `pyw`/`pythonw` commands, e.g.:
```bat
%PY% -c "import sys,os; os.environ.setdefault('PYTHONDONTWRITEBYTECODE','1')" 2>nul
start "" %PY% "%APPDIR%\caption.py"
```
(launching with plain `python`/`py` briefly flashes a console, but that's strictly better than a
silent failure, and can be hidden later with `pythonw -c` detection kept as the preferred path).

### 2. (Medium confidence, environmental, hard to confirm without the Windows user) The idle auto-quit firing while the window is still open

`caption.py:788-795` (`watch_idle`) calls `SERVER.shutdown()` — which ends `serve_forever()` and
exits the whole `pythonw` process — if no page has called `/api/ping` for `IDLE_QUIT = 180`
seconds (now 600, see "What's been fixed so far"). The page calls `ping()` every 15 seconds
(`app.html:2672`) while open. If those pings stop reaching the server for that long **while the
browser window still looks open to whoever's using it**, the app will quietly disappear from under
them — which matches "opens and then crashes" quite well (mid-use, then it's just gone). Ways this
could happen specifically on Windows and not Mac:
- A system proxy (common on managed/campus Windows machines, e.g. via Group Policy or a security
  agent that injects a loopback proxy for traffic inspection) intercepting `127.0.0.1` requests —
  `urllib`/browser `fetch` would then either hang or fail depending on the proxy's handling of
  localhost, differently from Mac.
- The app-mode window losing focus (minimized, alt-tabbed away) triggers background-tab timer
  throttling in Chromium-based browsers, which can push `setInterval` intervals out past a minute
  under memory/CPU pressure — more aggressive on Windows laptops running on battery/power-saving
  mode than on a Mac.
- Windows Defender / antivirus "web protection" briefly intercepting or delaying localhost HTTP
  requests.

This is **the one hypothesis I'd bet on**, but I want to be explicit that it's a hypothesis built
from reading `watch_idle()`'s logic, not something I observed happening.

**Minimal fix (defensive, regardless of root cause):** don't let a 3-minute quiet period kill a
session that's actually in use. Options, smallest first:
- Raise `IDLE_QUIT` (e.g. to 900s) — cheap, doesn't fix the underlying ping failure but gives much
  more margin.
- Have `watch_idle` log *why* it's quitting (it already prints to stderr, which is invisible under
  `pythonw` — redirect `sys.stderr`/`sys.stdout` to a log file in `%TEMP%` at startup so this and
  any other silent failure become visible). This single change (a few lines in `main()`) would
  immediately turn "it crashes, no idea why" into an actual error message you can read.

### 3. (Low-medium confidence) `pythonw` swallows every unguarded exception with zero visible error

Everything inside `main()` is wrapped (`caption.py:847-852`) and shows a `MessageBoxW` on failure —
good. But that wrapper only covers `main()`. The three top-level `import imgmeta` / `import
ingest` / `import teach` statements (`caption.py:36-38`) run **before** that `try/except` even
exists, and I did not find anything in those three modules that would only fail on Windows at
import time (no un-gated platform-specific top-level code, no third-party imports at module
level — Pillow is imported lazily inside `pillow()`). So I rate this low confidence as the actual
cause, but it's worth fixing regardless: wrap the imports too, so *any* future import-time problem
(e.g. a bad pip install leaving a broken `.pyc`, or a corrupted file from however the app is
distributed to the team) shows a message box instead of silently doing nothing.

**Minimal fix:** move the three imports inside `main()`'s existing try, or wrap them in their own
try/except that calls `tell_user()`.

### What I'd actually do first (cheaper than guessing further)

Redirect stdout/stderr to a log file before anything else runs, so the *next* time it "crashes" on
Windows there's a trace instead of nothing.

**Update — implemented and tested on a real Windows 11 laptop, in a follow-up session.** This and
the other three mitigations below are now in `caption.py` / `Caption Images (Windows).bat`:

- **Logging** (`%TEMP%\caption-images.log`), placed before the `imgmeta`/`ingest`/`teach`/
  `jersey_numbers_win` imports so it also catches import-time failures (hypothesis 3). Verified:
  deliberately broke one of the imported files and confirmed the log captured the exact error
  instead of the process dying with zero trace, which is exactly what used to happen.
- **Import-time failures now show a message box too** (hypothesis 3), not just failures inside
  `main()` — same verification as above.
- **The `.bat` no longer gives up if `pyw`/`pythonw` aren't on `PATH`** (hypothesis 1): it now
  looks for `pythonw.exe` next to whichever `python`/`py` it already found, and as a last resort
  opens with a visible console rather than failing silently.
- **`IDLE_QUIT` raised from 180s to 600s, and both the idle-shutdown and an explicit quit now log
  why they're closing** (hypothesis 2), e.g. `Closing: no page has checked in for 611 seconds
  (limit 600).` vs `Closing: quit requested from the page.` — so the log can now tell the
  difference between "the app decided to close itself" and "it just vanished," which it couldn't
  before.

**Two other classic Windows/`pythonw` gotchas were checked directly on real hardware and ruled
out** (on this machine/Python version, at least — can't promise the same on whatever the Windows
user's own machine is running):
`print()` to a `None` stream (which is what `sys.stdout`/`sys.stderr` actually are under a
truly detached `pythonw`, confirmed empirically) is a documented no-op in CPython, not a crash;
and `subprocess.Popen()` without explicit stdio redirection — the exact pattern `open_window()`
uses to launch Edge/Chrome — also didn't raise under a detached `pythonw` here, contrary to an
older, once-common Windows gotcha.

None of this *proves* what caused the original report — there's still no log from the actual
crash — but the next time it happens, `%TEMP%\caption-images.log` should say why, and several
plausible silent-failure paths are now closed off either way.

### Further checks, all on the real Windows 11 laptop (nothing more found, but worth recording)

- **Non-ASCII names, captions, folder paths, emoji** — a realistic risk given this is a college
  roster with international athletes, and Windows' default console/text codec (`cp1252`) isn't
  UTF-8. Tested end-to-end through the real running server: a folder named `Café José García —
  Senior Night`, a caption of `José García and Søren Müller celebrate with the 🏀 trophy — a huge
  win!`, a unicode metadata template name, all round-tripped correctly through the actual JPEG's
  XMP/EXIF/IPTC and through `Metadata Templates/*.json`, verified by independently re-reading the
  file in a separate process. **No bug found** — `imgmeta.py` already writes everything as UTF-8
  explicitly, and the file I/O throughout the app already specifies `encoding="utf-8"` rather than
  relying on Windows' non-UTF-8 default. The one real `UnicodeEncodeError` hit during this testing
  was in my own ad-hoc test script printing an emoji to a `cp1252` console — not in the app, which
  never prints raw user text to an unredirected stream (checked every `print()` call in
  `caption.py` directly).
- **Sustained use** — simulated processing a full game's worth of jersey-number reads (60
  sequential OCR calls in one long-running process, the same engine-reuse pattern the real server
  uses). Memory rose once from ~18MB to ~110MB when the OCR models first loaded, then stayed flat
  for all 60 reads — no leak — and timing stayed consistent (~3s/read) with no slowdown over the
  run.
- **"Mark of the Web"** — whoever hit this on Windows almost certainly got the app via email, a
  shared drive, or a download link rather than `git clone` (Coyle is on a Mac and can't have
  tested the Windows path himself), and Windows treats internet-downloaded files differently (an
  Explorer security warning, or SmartScreen, before they'll run). Simulated this by manually
  attaching a `Zone.Identifier` alternate data stream (`ZoneId=3`, with a fake `mail.google.com`
  referrer) to a copy of the `.bat` and launching it the normal way. **It ran with no warning or
  block** on this machine's default settings — ruled out as the cause here, though a
  corporate-managed Windows install (e.g. a BGSU-IT-imaged machine) with stricter Attachment
  Manager policy could still behave differently, so this isn't a 100% guarantee.

## Why it's fast and reliable on Mac

- **Jersey-number reading**: Apple Vision's on-device text recognition (`VNRecognizeTextRequest`,
  accurate mode) plus pose/body/face detection (`VNDetectHumanBodyPoseRequest` and friends,
  referenced in `jersey-numbers.m:192`) run on the Neural Engine via a compiled native binary —
  no Python overhead, no model download, no network call. The personal classifier (trained with
  `jersey-train`, via `CreateML`/`CoreML`) also runs through Vision's `VNCoreMLRequest`, again
  Neural-Engine-accelerated.
- **Previews/resizing**: macOS's built-in `sips` command-line tool (`imgmeta.py:1120-1134`,
  `1154-1201`) does hardware-accelerated JPEG decode/resize/rotate without any Python image
  library — it's part of the OS, always present, and tuned for Apple's hardware (including RAW
  decoding paths for previews).
- **Folder dialogs / file I/O**: native `osascript`/Finder integration and typically local
  SSD + unified memory, so copying large RAW/JPEG pairs off a card is bottlenecked by the card
  reader, not the Mac.

Equivalents for a cross-platform or Windows-first rewrite of just the parts that currently assume
macOS:
| Mac piece | Role | Windows/Linux equivalent |
|---|---|---|
| Apple Vision OCR (jersey numbers) | reads jersey numbers | Tesseract or EasyOCR/PaddleOCR (slower, worse on small/angled jersey digits without tuning); Windows.Media.Ocr (Windows-only, UWP API, usable from Python via `winrt` bindings, but adds real complexity); a small ONNX digit-classifier run via ONNX Runtime (CPU, or DirectML/CUDA for speed) |
| CoreML / CreateML (personal number model) | per-team fine-tuned classifier | Export the same training data to a small ONNX/PyTorch classifier; ONNX Runtime with DirectML (Windows GPU-agnostic) or CUDA (NVIDIA) for inference |
| `sips` | preview/resize/rotate | Pillow (already wired up as the fallback) — this one's basically already solved |
| `osascript` folder picker | native folder dialog | `tkinter.filedialog` (already wired up) |

Note there's no "Neural Engine vs CPU" speed gap in the *metadata* workflow (ingest/cull/caption) —
that's pure Python file I/O and byte-level parsing, identically fast on both OSes. The speed/quality
gap that matters is narrow and specific: jersey-number OCR accuracy and latency.

## Docker assessment

Short version: **Docker is not a good fit for this app as a whole**, and would not have prevented
or would not fix the Windows crash — it solves a different problem (environment consistency) than
the one in front of you (an unexplained crash on one specific OS) and would *remove* a feature
(jersey-number OCR) that currently exists on Mac.

- **It's a desktop GUI app, not a server.** The entire point of `caption.py` is to pop open a
  native-feeling browser window pointed at `127.0.0.1`, call the OS folder picker, detect mounted
  memory cards (`/Volumes/*` or drive letters), and copy files between the card and arbitrary
  folders on the host filesystem. None of that maps cleanly into a container: a container doesn't
  see the host's mounted SD card, the host's `Program Files`, or the host's folder-picker dialog
  without bind-mounting specific paths in advance (which defeats "point it at whatever card/folder
  you plug in today").
- **macOS Docker runs a Linux VM** — Apple Vision, CoreML, and the Neural Engine are unavailable
  inside it. Containerizing even just the Mac build would mean losing jersey-number reading
  entirely, the one feature that's actually hard to replicate elsewhere. You'd gain nothing (the
  rest of the app already runs identically on both OSes in plain Python) and lose that one feature.
- **Windows Docker (WSL2)** needs the NVIDIA Container Toolkit for GPU passthrough, and a typical
  team Windows laptop is unlikely to have an NVIDIA GPU dedicated to this (and the current Windows path
  doesn't use a GPU at all — it's CPU-only Pillow). So containerizing the Windows side buys nothing
  either; CPU-only Pillow runs exactly as fast in a container as outside one, with extra overhead
  for Docker Desktop itself.
- **File access** is the biggest practical blocker regardless of OS: card detection and "copy off
  the card" is the app's core job, and a container sandboxes exactly the thing you need unrestricted
  access to (arbitrary drive letters/volumes chosen at runtime by a non-technical photographer).
- **Distribution experience** for a non-technical photo team gets *worse*, not better: today it's
  "double-click the app" (Mac) / "double-click a .bat" (Windows). Docker would mean "install Docker
  Desktop, understand containers enough to troubleshoot it, bind-mount the right drive." That's a
  regression for the actual users of this tool.

Where Docker **would** make sense: if you ever build a genuinely headless/server mode — e.g. a
batch captioning or metadata-templating service that runs on a lab server and processes a folder
dropped into it, with no GUI, no card detection, no folder picker, no jersey-number feature. That's
a plausible *future* product, but it's not this app, and building it wouldn't fix today's Windows
crash.

## Compare alternatives

**a. Per-platform backend abstraction (Apple Vision on Mac, ONNX/CUDA/Tesseract elsewhere), selected at runtime**
This is essentially *already how the code is structured* for the jersey-OCR feature — `numbers_available()`/`teach.available()` already gate a Mac-only backend, cleanly, with a working fallback (`{"error": "needs a Mac."}`). The only gap is that there is no non-Mac backend at all (feature is absent, not degraded). Adding a real Windows backend (ONNX digit classifier, or Tesseract) behind the same `numbers_available()`-style switch is a contained, additive change — it touches only `caption.py`'s API surface and a new `teach_windows.py`/equivalent, not the metadata/ingest code. **Recommended if jersey-number reading on Windows ever becomes a real requirement** — but note it's not blocking the crash fix at all, and is a separate, larger project (training/evaluating a cross-platform OCR+classifier pipeline that's good enough on jersey numbers specifically).

**b. Replace the Apple-specific parts with one cross-platform stack everywhere**
Would mean giving up Apple Vision's free, fast, accurate, on-device OCR on Mac in exchange for parity — almost certainly a regression in OCR quality/speed for the majority current (Mac) users, to fix a Windows-only bug that isn't actually caused by the Apple-specific code (it's gated off already). Not recommended; solves the wrong problem.

**c. Docker for server/CLI use only**
Reasonable later, for a hypothetical headless batch-processing mode — see Docker assessment above. Not useful for the crash, not useful for the desktop app as it exists today.

**d. PyInstaller-style packaging + CI testing on Windows/macOS/Linux**
This is the one that actually addresses "it broke on an OS nobody tested" as a *process* problem, not just this one bug. The app is currently distributed as raw source + a `.bat`/`.app` wrapper that depends on whatever Python happens to be on the user's `PATH` — exactly the kind of thing that's fragile in the way hypothesis #1 above describes. A GitHub Actions matrix (`windows-latest`, `macos-latest`) that at minimum imports `caption.py`/`imgmeta.py`/`ingest.py`/`teach.py` and runs any unit tests headlessly (`--no-browser`) on every push would have caught an import-time Windows failure immediately, and costs very little to set up given there are no third-party dependencies to install (aside from Pillow/pillow-heif, which CI can install the same way). **Recommended, independent of the crash fix** — it's cheap insurance against this happening again on either OS.

## Recommended path, in priority order

Steps 1-4 and the confirmed port-reuse bug (see above) are **done**, on the `dawson` branch, as of
the follow-up Windows testing session. Since Coyle can't run Windows himself to verify any of
this, "done" here means tested directly on a real Windows 11 laptop in this session, not confirmed
by Coyle — that's the gap step 5 is about closing.

1. ~~Add stdout/stderr logging to a file on Windows~~ **Done.** `%TEMP%\caption-images.log`.
2. ~~Harden the `.bat` launcher~~ **Done, then superseded by something stronger.** First hardened the fallback if `pyw`/`pythonw` weren't found; then, once it became clear the real report was "couldn't even open it" at all (pointing at machines with no real Python, not just a missing launcher variant), replaced the whole system-Python dependency with a bundled interpreter instead — see "Root-cause theory, now fixed" above.
3. ~~Raise `IDLE_QUIT` and log why the server shuts down~~ **Done.** 180s → 600s, both idle-quit and explicit quit now log why.
4. ~~Guard the top-level imports~~ **Done.** Import failures now show a message box, not silence.
5. **(Next — this is the actual remaining gap)** Get this branch in front of whoever actually uses
   Windows for this (not Coyle — he's on a Mac). If it crashes again, `%TEMP%\caption-images.log`
   should now say why; if it doesn't, that's meaningful evidence too, given the confirmed
   port-reuse bug this branch fixes. Either way, this is the step that needs a human with a
   Windows machine who isn't this session's test laptop.
6. **(Medium-term, independent track)** Set up CI on Windows + macOS (alternative **d** above) so the next regression is caught before it reaches a game, on whichever OS.
7. **(Only if/when the team actually wants jersey-number reading on Windows)** Already shipped on this branch (free OCR via `jersey_numbers_win.py`) — training a personal model on Windows (alternative **a**'s remaining piece) is still a separate, bigger project if ever wanted.
8. **Do not pursue Docker** for the app as it exists (see assessment above); revisit only if a headless/server mode becomes an actual product need.

## Open questions for whoever actually hit this on Windows

Coyle develops on a MacBook and can't run the Windows build himself, so these need to reach
whichever teammate originally reported "it crashes" — he can relay them but can't answer directly.

1. **What does "opens and then crashes" literally look like?** Does the browser window/UI ever appear? If yes, how long does it stay usable before it breaks (seconds? minutes? after a specific click)? If no, does even a console window flash briefly? This single answer would let me collapse the three ranked hypotheses above into one.
2. Is there any visible error — a Windows error dialog, a browser "can't connect" page, or just the window disappearing with no message at all?
3. How is Python installed on the Windows machine — python.org installer, Microsoft Store, or something else (e.g. via a campus IT image)? Does typing `py --version` and `pyw` (with no arguments) in a `cmd` window work?
4. Is this Windows machine on BGSU's campus network/VPN, or behind any IT-managed proxy or antivirus "web protection" product? (Relevant to hypothesis #2 — localhost traffic interception is the kind of thing campus security tooling sometimes does.)
5. Does the crash happen immediately on first launch, or only after the app has been open and in use for a few minutes?
6. Has the Windows `.bat` actually completed the one-time Pillow/pillow-heif install successfully (i.e., does `python -c "import PIL, pillow_heif"` succeed in a terminal on that machine)?
