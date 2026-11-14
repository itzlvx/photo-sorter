@echo off
rem run-tests: launcher for scripts\run_tests.py (needs Python 3.9 or newer; set PHOTO_SORTER_PYTHON to choose one).
call "%~dp0scripts\find-python.bat"
if not defined PY (
  echo Python 3.9 or newer was not found. Install it from python.org ^(tick "Add python.exe to PATH"^), or set PHOTO_SORTER_PYTHON to its path.
  exit /b 1
)
%PY% "%~dp0scripts\run_tests.py" %*
exit /b %errorlevel%
