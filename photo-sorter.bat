@echo off
rem photo-sorter: launcher for scripts\menu.py (needs Python 3.9 or newer; set PHOTO_SORTER_PYTHON to choose one).
rem It first brings this copy up to date with the main branch on GitHub; set PHOTO_SORTER_NO_UPDATE=1 to skip that.
rem The update and the restart share one line on purpose: an update can change this file while it runs, so the old
rem copy must not read any further. The restarted copy skips the update and clears the flag.
setlocal
if not defined PHOTO_SORTER_UPDATED call "%~dp0scripts\self-update.bat" & if errorlevel 3 (set "PHOTO_SORTER_UPDATED=1" & "%~f0" %*)
set "PHOTO_SORTER_UPDATED="
rem Option 1 (the web interface) listens on the network so other computers can open it; they must sign in as admin.
rem Set PHOTO_SORTER_UI_ARGS yourself to change this, for example to --host 127.0.0.1 for this computer only.
if not defined PHOTO_SORTER_UI_ARGS set "PHOTO_SORTER_UI_ARGS=--host 0.0.0.0"
call "%~dp0scripts\find-python.bat"
if not defined PY (
  echo Python 3.9 or newer was not found. Install it from python.org ^(tick "Add python.exe to PATH"^), or set PHOTO_SORTER_PYTHON to its path.
  exit /b 1
)
%PY% "%~dp0scripts\menu.py" %*
exit /b %errorlevel%
