# Caption Images

A Photo Mechanic replacement for the BG Falcon Media photo team. It ingests photos off a card, culls them, and captions them. It runs as a small local web app: a Python server (standard library only) plus one HTML page, opened in its own window. Mac and Windows.

For how to *use* it, see `HOW TO USE.txt` and `Caption Images - Team Guide.pdf`. This file is the developer handoff: how it's built, what we decided, and what's still open.

## Running it

- **Mac:** double-click `Caption Images.app`. The launcher in `Contents/MacOS/` (a universal binary) starts `Contents/Resources/caption.py`.
- **Windows:** `Caption Images (Windows).bat` runs the same `caption.py` with `pyw`/`pythonw`. On first run it pip-installs Pillow and pillow-heif (optional; used for smaller copies and HEIC previews) and RapidOCR (optional; used for jersey-number reading — see `jersey_numbers_win.py`). All three fail gracefully if there's no internet the first time; the app still opens, just without those features.
- **From a terminal:** `python3 "Caption Images.app/Contents/Resources/caption.py" [folder]`

The server listens on `127.0.0.1:8765` only. `/api/ping` returns `APP_VERSION` (currently 6, in `caption.py`). If the page sees a newer version than the running server, it shows an "updated while it was open, Quit now" banner. **Bump `APP_VERSION` whenever you change the Python.**

### Testing without breaking the live app

Coyle uses the app at games. `app.html` is re-read on every page load, so editing the installed copy goes live immediately. Work in this repo (or another copy), change `PORT` to something like `8798` while testing, and set it back to `8765` before installing. Don't bulk-read photos stored only in iCloud; it triggers downloads.

## Files

| Path | What it is |
|---|---|
| `Contents/Resources/caption.py` | HTTP server and API (`Handler` class). Folder picking, shots list, code replacements, templates, tags, Lightroom hand-off, export, quit. |
| `Contents/Resources/app.html` | The whole UI (Ingest, Cull, Caption, Teach numbers), about 2,700 lines of HTML/CSS/JS in one file. |
| `Contents/Resources/imgmeta.py` | Reads and writes metadata with no dependencies: XMP, IPTC, EXIF in JPEG; PNG text chunks; `.xmp` sidecars for RAW/HEIC/TIFF. Extracts embedded JPEG previews from RAW (CR3 parsing included). |
| `Contents/Resources/ingest.py` | Card detection (folders with `DCIM`), copy with renaming, apply metadata template, Getty caption formatting, copy/move/export jobs. Never deletes or changes files on the card. Remembers what's been ingested in `~/.caption-images-ingested.json`. |
| `Contents/Resources/teach.py` | "Teach numbers": labelling jersey crops and training a personal model. Data lives in `~/Library/Application Support/Caption Images/Numbers`. Mac only. |
| `Contents/Resources/jersey-numbers.m` / `jersey-numbers` | Objective-C tool using Apple Vision to read jersey numbers (pose → torso crop → OCR, plus a tile sweep, voting). Prints JSON. The compiled binary is checked in. Mac only. |
| `Contents/Resources/jersey-train.swift` / `jersey-train` | Trains the Core ML number model from labelled crops. The compiled binary is checked in. Mac only. |
| `Contents/Resources/jersey_numbers_win.py` | Reads jersey numbers on Windows: free, offline OCR (RapidOCR/ONNX Runtime, pip-installed by the `.bat`) over the whole photo plus a tile sweep, with the same read-and-vote idea as the Mac tool. No pose/player detection (nothing free to do that with), so no torso crop and no personal-model training on Windows — "Teach numbers" training stays Mac only. See its docstring for the exact tradeoffs. |
| `Code Replacements/*.txt` | Photo Mechanic–style code files, one `code<TAB>replacement` per line. One per sport, plus `News.txt` and `DATES.txt` (dates, datelines, photo-credit codes). |
| `Metadata Templates/Generic.json` | The shared ingest/caption template. Personal templates are saved next to it but are git-ignored. |
| `Tags.txt` | Keyword list offered in the UI. |
| `CodeReplacementsGenerator.xlsx` | Spreadsheet used to build roster code files. |

RAW files are never modified; their metadata goes into a sidecar with the same name.

## Decisions made so far

- **Photo Mechanic parity**, based on PM 2026.6's own keymaps and XMP names: T to tag (`photomechanic:Tagged`), color classes (`photomechanic:ColorClass`), `photomechanic:Prefs` kept in sync, PM's three number-key presets (chosen in the ? sheet), G for the contact sheet, multi-select, and tagged/color filters.
- **Ratings:** `xmp:Rating` 1–5, −1 = rejected. If there's no sidecar rating, the in-camera star is read (Canon R5 CR3 embedded XMP, JPEG EXIF 0x4746). Clearing a rating writes 0. Ingest copies the in-camera star into new sidecars.
- **Getty format** (template checkbox). It turns a plain sentence into `CITY, STATE - MONTH DD: <sentence> on Month D, YYYY at <Venue> in City, State. (Name/Credit)`. The header date is zero-padded and the body date isn't. The venue is typed as "the Stroh Center" but stored in Location without "the". Implemented twice, and the two must match: `ingest.getty_caption()` and `gettyCaption()` in `app.html`.
- **Template fill-ins:** `{dateline}` `{gettydate}` `{place}` `{venue}` `{photoby}` `{photographer}` `{event}` `{year}`. Generic uses `{photographer}` so each teammate gets their own name.
- **Credit/Source** is "BG Falcon Media". Credit codes in `DATES.txt` are initials + `bg` (clashing initials use two letters of the first name: `avtbg`/`antbg`, `casbg`/`cosbg`, `jesbg`/`josbg`).
- **Previews:** 2000 px for Cull, 320 px thumbnails, cache pruned at 14 days / 3 GB. Web export is 2000 px.
- **Templates are files** (`/api/templates`, `/api/templates/save`, `/api/templates/delete`), so they can be shared. Templates saved in the browser by older versions are moved over once.

## Open items

- Photo Mechanic hasn't been checked to confirm it shows our tags and color classes.
- **The Windows version opens and then crashes** (reported by Coyle). **Found and fixed one real, reproduced Windows-only bug**: `http.server`'s default socket option (`SO_REUSEADDR`) behaves very differently on Windows than Mac/Linux — two near-simultaneous launches could both end up listening on port 8765 with different tokens, making saves/ratings/captions randomly fail depending on which process answered a given request. Fixed (`Server.allow_reuse_address = False` in `caption.py`) and re-verified under the original race condition. **Can't yet say this was *the* crash Coyle saw** — there's still no log from his actual crash to compare against. Also done: `caption.py` logs to `%TEMP%\caption-images.log` so the next crash leaves a trace, import failures show a message box instead of failing silently, the `.bat` no longer gives up if `pyw`/`pythonw` aren't on `PATH`, and idle auto-quit (180s → 600s) logs why it closed. See `INVESTIGATION.md` for the full writeup.
- Jersey-number *reading* now works on Windows too (`jersey_numbers_win.py`, free/offline OCR — see its docstring and `INVESTIGATION.md` for why this approach and what's different from the Mac tool). "Teach numbers" *training* is still Mac only; a Windows trainer would be a separate project if the team wants it.
- In January 2027, update the officials: switch `\gov\` to Amy Acton and `\wcclee\` to Ryan Lee in `News.txt`, and remove Herringshaw.
- The new sport files (baseball, softball, gymnastics, swimming and diving, track and field, tennis, golf) have no rosters yet.
- 30 of the 42 photo-credit names came from an email recipient list, so preferred names aren't confirmed.
- The source for the Mac launcher binary (`Contents/MacOS/Caption Images`) isn't in the repo.

## Privacy

This repo is **private**. The code files contain student-athlete rosters and team members' names, so don't make it public or copy those files anywhere public.
