@echo off
rem Brings this copy up to date with the main branch on GitHub (used by photo-sorter.bat before the menu starts).
rem It only fast-forwards: your own changes and commits are never touched, and it never stops the program starting.
rem Skipped when PHOTO_SORTER_NO_UPDATE is set, this folder is not a git checkout, or git is not installed.
rem Exit code 3 means new files were downloaded, so the caller should start again from the new copy.
setlocal
if defined PHOTO_SORTER_NO_UPDATE exit /b 0
set "ROOT=%~dp0.."
if not exist "%ROOT%\.git" exit /b 0
where git >nul 2>&1
if errorlevel 1 exit /b 0
set "BRANCH="
for /f "delims=" %%b in ('git -C "%ROOT%" rev-parse --abbrev-ref HEAD 2^>nul') do set "BRANCH=%%b"
if /i not "%BRANCH%"=="main" (
  echo Not checking for updates: this copy is on branch "%BRANCH%", not main.
  exit /b 0
)
echo Checking GitHub for a newer version...
git -C "%ROOT%" fetch --quiet origin main
if errorlevel 1 (
  echo Could not reach GitHub, so starting the version you have.
  exit /b 0
)
set "OLD="
set "NEW="
for /f %%h in ('git -C "%ROOT%" rev-parse HEAD') do set "OLD=%%h"
for /f %%h in ('git -C "%ROOT%" rev-parse origin/main') do set "NEW=%%h"
git -C "%ROOT%" merge-base --is-ancestor %NEW% %OLD%
if not errorlevel 1 (
  echo Already up to date.
  exit /b 0
)
git -C "%ROOT%" merge-base --is-ancestor %OLD% %NEW%
if errorlevel 1 (
  echo Not updating: this copy has its own commits that are not on GitHub. Run "git pull" yourself.
  exit /b 0
)
git -C "%ROOT%" diff --quiet HEAD
if errorlevel 1 (
  echo Not updating: files in this copy were changed. Save or undo your changes, then run "git pull".
  exit /b 0
)
rem The merge rewrites this file, and cmd reads a .bat file as it goes, so everything from here on is one block.
(
  git -C "%ROOT%" merge --ff-only --quiet origin/main
  if errorlevel 1 (
    echo The update could not be applied, so starting the version you have.
    exit /b 0
  )
  echo Updated to the latest version:
  git -C "%ROOT%" log --oneline --no-decorate -n 20 %OLD%..%NEW%
  for /f "delims=" %%f in ('git -C "%ROOT%" diff --name-only %OLD% %NEW% -- "requirements*.txt"') do echo The package list %%f changed. If something reports a missing package: .venv\Scripts\python -m pip install -r %%f
  exit /b 3
)
