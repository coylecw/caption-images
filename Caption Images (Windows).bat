@echo off
REM Double-click to open Caption Images. It opens in its own window.
REM (The program files are inside the "Caption Images.app" folder, shared with the Mac version.)
REM
REM This uses its own copy of Python (in "python-windows", next to this file) instead of
REM whatever's on this PC. Windows 11 machines that have never had Python installed often have
REM a "python"/"python3" stand-in that just opens the Microsoft Store and does nothing when run -
REM so depending on the system's Python silently failed here for anyone who'd never installed
REM Python themselves. Carrying our own means it always works, the same way on every PC.
set "APPDIR=%~dp0Caption Images.app\Contents\Resources"
set "PYDIR=%APPDIR%\python-windows"
set "PY=%PYDIR%\python.exe"
set "PYW=%PYDIR%\pythonw.exe"
set PYTHONDONTWRITEBYTECODE=1

if not exist "%PY%" (
  echo Caption Images is missing its bundled Python ^(expected at "%PYDIR%"^).
  echo Try getting a fresh copy of Caption Images.
  pause
  exit /b 1
)

REM One-time setup: Pillow makes smaller copies and HEIC previews work on Windows.
REM If it can't be installed (no internet), the app still opens without those two things.
"%PY%" -c "import PIL, pillow_heif" >nul 2>nul
if errorlevel 1 (
  echo Setting up Caption Images. This only happens once...
  "%PY%" -m pip install --quiet --disable-pip-version-check pillow pillow-heif
)

REM Also one-time: RapidOCR reads jersey numbers on Windows (free, offline, no account or GPU
REM needed). It's a bigger download than Pillow, so this may take a minute the first time.
REM If it can't be installed, the app still opens; jersey-number suggestions just won't appear.
"%PY%" -c "import rapidocr_onnxruntime" >nul 2>nul
if errorlevel 1 (
  echo Setting up jersey-number reading. This only happens once and may take a minute...
  "%PY%" -m pip install --quiet --disable-pip-version-check rapidocr-onnxruntime
)

start "" "%PYW%" "%APPDIR%\caption.py"
