@echo off
rem Sets PY to a Python 3.9+ command (used by the other .bat files). The variable is kept on purpose.
set "PY="
set "CAND="
if defined PHOTO_SORTER_PYTHON goto :custom
if exist "%~dp0..\.venv\Scripts\python.exe" call :try "%~dp0..\.venv\Scripts\python.exe"
if defined PY exit /b 0
if exist "%~dp0..\venv\Scripts\python.exe" call :try "%~dp0..\venv\Scripts\python.exe"
if defined PY exit /b 0
call :try py -3
if defined PY exit /b 0
call :try python
if defined PY exit /b 0
call :try python3
exit /b 0

:custom
set CAND=%PHOTO_SORTER_PYTHON%
%CAND% "%~dp0pycheck.py" >nul 2>&1
if not errorlevel 1 set PY=%PHOTO_SORTER_PYTHON%
exit /b 0

:try
set CAND=%*
if "%~1"=="%~f1" set CAND="%~1"
%CAND% "%~dp0pycheck.py" >nul 2>&1
if not errorlevel 1 set PY=%CAND%
exit /b 0
