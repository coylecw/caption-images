@echo off
REM Double-click to open Caption Images. It opens in its own window.
REM (The program files are inside the "Caption Images.app" folder, shared with the Mac version.)
set "APPDIR=%~dp0Caption Images.app\Contents\Resources"
set PYTHONDONTWRITEBYTECODE=1

set "PY="
where py >nul 2>nul && set "PY=py"
if not defined PY (
  where python >nul 2>nul && set "PY=python"
)
if not defined PY goto nopython

REM One-time setup: Pillow makes smaller copies and HEIC previews work on Windows.
REM If it can't be installed (no internet), the app still opens without those two things.
%PY% -c "import PIL, pillow_heif" >nul 2>nul
if errorlevel 1 (
  echo Setting up Caption Images. This only happens once...
  %PY% -m pip install --user --quiet --disable-pip-version-check pillow pillow-heif
)

REM Also one-time: RapidOCR reads jersey numbers on Windows (free, offline, no account or GPU
REM needed). It's a bigger download than Pillow, so this may take a minute the first time.
REM If it can't be installed, the app still opens; jersey-number suggestions just won't appear.
%PY% -c "import rapidocr_onnxruntime" >nul 2>nul
if errorlevel 1 (
  echo Setting up jersey-number reading. This only happens once and may take a minute...
  %PY% -m pip install --user --quiet --disable-pip-version-check rapidocr-onnxruntime
)

where pyw >nul 2>nul
if not errorlevel 1 (
  start "" pyw "%APPDIR%\caption.py"
  exit /b 0
)
where pythonw >nul 2>nul
if not errorlevel 1 (
  start "" pythonw "%APPDIR%\caption.py"
  exit /b 0
)

REM Neither pyw nor pythonw is on PATH (this happens with some Python installs, e.g. the
REM Microsoft Store one, or python.org without "Install launcher for all users"). %PY% itself
REM works, though, so look for pythonw.exe next to it instead of giving up.
set "PYW="
for /f "delims=" %%P in ('%PY% -c "import sys,os;print(os.path.join(os.path.dirname(sys.executable),'pythonw.exe'))" 2^>nul') do set "PYW=%%P"
if defined PYW if exist "%PYW%" (
  start "" "%PYW%" "%APPDIR%\caption.py"
  exit /b 0
)

REM Last resort: open with a visible console rather than not open at all.
echo Couldn't find a windowed Python (pyw/pythonw); opening with a console instead...
start "Caption Images" %PY% "%APPDIR%\caption.py"
exit /b 0

:nopython
echo Caption Images needs Python. Install it from https://www.python.org/downloads/
echo (tick "Add Python to PATH"), then double-click Caption Images again.
pause
