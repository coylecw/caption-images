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

:nopython
echo Caption Images needs Python. Install it from https://www.python.org/downloads/
echo (tick "Add Python to PATH"), then double-click Caption Images again.
pause
