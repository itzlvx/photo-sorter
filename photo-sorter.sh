#!/usr/bin/env bash
# Photo sorter menu.
#   ./photo-sorter.sh          show the menu
#   ./photo-sorter.sh ui       start the web interface straight away   (also: 1)
#   ./photo-sorter.sh sort     intake and sort photos                  (also: 2)
#   ./photo-sorter.sh faces    refresh, find faces, group people       (also: 3)
#   ./photo-sorter.sh metadata read camera details for indexed photos (also: 4)
#   ./photo-sorter.sh search   store search vectors for indexed photos (also: 5)
#   ./photo-sorter.sh clipcheck check (and download) a search model  (also: 6)
#
# On Windows run it from "Git Bash" (installed with Git for Windows).
# Settings (all optional):
#   PHOTO_SORTER_PYTHON   the Python to use, for example "py -3" or a path to python.exe
#   PHOTO_SORTER_DB       where the index database lives (default: a local app-data folder)
#   PHOTO_SORTER_UI_ARGS  extra options for the web interface, for example "--port 9000"
#   PHOTO_SORTER_FACES_ARGS  extra options for the face scan, for example "--det-size 960"

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/common.sh
. "$APP_DIR/scripts/common.sh"

DB_ARGS=()
if [ -n "${PHOTO_SORTER_DB:-}" ]; then DB_ARGS=(--db "$PHOTO_SORTER_DB"); fi

usage() { sed -n '2,16p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

ensure_modules() {   # ensure_modules "what needs them" requirements-file module...
  local label="$1" req="$2" mod; shift 2
  local missing=()
  for mod in "$@"; do "${PY[@]}" -c "import $mod" >/dev/null 2>&1 || missing+=("$mod"); done
  [ ${#missing[@]} -eq 0 ] && return 0
  warn "$label needs Python packages that are not installed: ${missing[*]}"
  if confirm "Install them now with pip ($req)?" y; then
    ( cd "$APP_DIR" && "${PY[@]}" -m pip install -r "$req" ) || { fail "pip could not install them."; return 1; }
    [ "$req" = requirements-faces.txt ] && say "Using an NVIDIA GPU? Follow 'Using your NVIDIA GPU' in README.md afterwards."
    return 0
  fi
  return 1
}

# ---------------------------------------------------------------- 1. web interface
action_ui() {
  header "Start the web interface"
  ensure_modules "The web interface" requirements-ui.txt fastapi uvicorn || return 1
  say "It opens in your browser, normally at http://127.0.0.1:8765. Press Ctrl+C here to stop it."
  # shellcheck disable=SC2086
  run_py ui ${DB_ARGS[@]+"${DB_ARGS[@]}"} ${PHOTO_SORTER_UI_ARGS:-}
}

# ---------------------------------------------------------------- 2. intake and sort
action_sort() {
  header "Intake and sort photos"
  local source dest rc
  local -a move=()
  ask_dir source "Where are the photos to sort? (searched recursively, q to cancel)" "$(conf_get LAST_SOURCE)" 1 || return 0
  ask_dir dest "Where should the sorted library be? (Year\\01-jan style folders go here, q to cancel)" "$(conf_get LIBRARY_DIR)" 0 || return 0
  say "Copy keeps your originals untouched. Move deletes each original only after the copy is checked."
  if confirm "Move the photos instead of copying them?" n; then move=(--move); fi
  conf_set LAST_SOURCE "$source"; conf_set LIBRARY_DIR "$dest"

  say "First a preview. Nothing is changed yet."
  run_py sort "$(to_native "$(to_unix "$source")")" "$(to_native "$(to_unix "$dest")")" ${move[@]+"${move[@]}"} ${DB_ARGS[@]+"${DB_ARGS[@]}"}
  rc=$?
  if [ "$rc" -ge 2 ]; then fail "The preview could not run (see above). Nothing was changed."; return 1; fi
  [ "$rc" -eq 1 ] && warn "The preview reported some errors (see above)."
  if ! confirm "Sort the photos like this?" n; then say "Nothing was changed."; return 0; fi

  run_py sort "$(to_native "$(to_unix "$source")")" "$(to_native "$(to_unix "$dest")")" ${move[@]+"${move[@]}"} ${DB_ARGS[@]+"${DB_ARGS[@]}"} --apply
  rc=$?
  if [ "$rc" -eq 0 ]; then ok "Sorting finished."; else warn "Sorting finished with problems (exit code $rc). Read the messages above."; fi
  say "Next: choose 3 to find the faces in the new photos."
  return "$rc"
}

# ---------------------------------------------------------------- 3. refresh, faces, people
action_faces() {
  header "Find faces and group people"
  say "This runs three steps: refresh the index from your sorted library, scan new photos for faces, group faces into people."
  say "Your photos are only read, never changed."
  local lib limit="" hw="" rc
  local -a limit_args=() hw_args=()
  ask_dir lib "Sorted library folder (q to cancel)" "$(conf_get LIBRARY_DIR)" 1 || return 0
  conf_set LIBRARY_DIR "$lib"

  printf 'How many photos should be scanned for faces? Enter = all new photos, or a number for a quick test: '
  IFS= read -r limit || limit=""
  limit="$(clean_path "$limit")"
  if [ -n "$limit" ]; then
    case "$limit" in
      *[!0-9]*|0) warn "That is not a number, so all new photos will be scanned."; limit="" ;;
      *) limit_args=(--limit "$limit") ;;
    esac
  fi
  printf 'Hardware: [a]uto (GPU if set up, else CPU), [g]pu only, [c]pu only. Enter = auto: '
  IFS= read -r hw || hw=""
  case "$(printf '%s' "$hw" | tr '[:upper:]' '[:lower:]')" in
    g|gpu) hw_args=(--gpu) ;;
    c|cpu) hw_args=(--cpu) ;;
  esac
  confirm "Start now?" y || { say "Nothing was changed."; return 0; }

  ensure_modules "Face scanning" requirements-faces.txt insightface onnxruntime || return 1

  say "Step 1 of 3: refresh the index"
  run_py refresh "$(to_native "$(to_unix "$lib")")" --apply ${DB_ARGS[@]+"${DB_ARGS[@]}"}
  rc=$?
  if [ "$rc" -ge 2 ]; then fail "The refresh could not run. Stopping."; return 1; fi
  if [ "$rc" -eq 1 ]; then confirm "The refresh reported some errors. Continue anyway?" n || return 1; fi

  say "Step 2 of 3: scan for faces (progress is saved, so you can stop with Ctrl+C and run this again)"
  # shellcheck disable=SC2086
  run_py faces --apply ${limit_args[@]+"${limit_args[@]}"} ${hw_args[@]+"${hw_args[@]}"} ${DB_ARGS[@]+"${DB_ARGS[@]}"} ${PHOTO_SORTER_FACES_ARGS:-}
  rc=$?
  if [ "$rc" -eq 130 ]; then warn "Stopped. Run this option again to continue where it left off."; return 130; fi
  if [ "$rc" -ge 2 ]; then fail "Face scanning could not run. Stopping."; return 1; fi
  [ "$rc" -eq 1 ] && warn "Some photos could not be scanned (see above). Going on with the rest."

  say "Step 3 of 3: group faces into people"
  run_py cluster --apply ${DB_ARGS[@]+"${DB_ARGS[@]}"}
  rc=$?
  if [ "$rc" -ne 0 ]; then fail "Grouping did not finish (exit code $rc)."; return "$rc"; fi
  ok "Done. Choose 1 to open the web interface and name the people."
}

# ---------------------------------------------------------------- 4. camera details
action_metadata() {
  header "Read camera details"
  say "Reads camera, lens, exposure, size and GPS position for photos already in the index. Your photos are only read, never changed."
  say "New photos get these automatically when they are sorted or refreshed; this is for catching up or reading everything again."
  local rc
  local -a reread=()
  if confirm "Read every photo again, not only the ones without details?" n; then reread=(--reread); fi
  say "First a preview. Nothing is changed yet."
  run_py metadata ${reread[@]+"${reread[@]}"} ${DB_ARGS[@]+"${DB_ARGS[@]}"}
  rc=$?
  if [ "$rc" -eq 2 ]; then fail "There is no index yet. Choose 2 to sort photos or 3 to refresh your library first."; return 1; fi
  if [ "$rc" -ne 0 ]; then fail "The preview could not run (see above). Nothing was changed."; return 1; fi
  confirm "Read them now?" y || { say "Nothing was changed."; return 0; }
  run_py metadata --apply ${reread[@]+"${reread[@]}"} ${DB_ARGS[@]+"${DB_ARGS[@]}"}
  rc=$?
  case "$rc" in
    0)   ok "Done. The details show in the photo viewer of the web interface." ;;
    130) warn "Stopped. Run this option again to continue where it left off." ;;
    1)   warn "Some photos could not be read (see above). The rest were saved." ;;
    *)   fail "Reading camera details did not finish (exit code $rc)." ;;
  esac
  return "$rc"
}

# ---------------------------------------------------------------- 5. search vectors
DEFAULT_SEARCH_MODEL="ViT-L-14-quickgelu__dfn2b"
MODEL_NOT_DOWNLOADED=3   # embed's exit code when the model files are not there yet

action_search() {
  header "Prepare search"
  say "Stores a search vector for every photo in the index, so you can later find photos by typing a description."
  say "Your photos are only read, never changed. It uses the NVIDIA GPU if it is set up, and can be stopped with Ctrl+C and run again."
  ensure_modules "Search" requirements-search.txt tokenizers || return 1
  local model default rc
  default="$(conf_get SEARCH_MODEL)"; default="${default:-$DEFAULT_SEARCH_MODEL}"
  printf 'Search model (Enter = %s; or another, for example ViT-B-16-SigLIP2__webli): ' "$default"
  IFS= read -r model || model=""
  model="$(clean_path "$model")"; model="${model:-$default}"
  say "First a preview. Nothing is changed yet."
  run_py embed --model "$model" ${DB_ARGS[@]+"${DB_ARGS[@]}"}
  rc=$?
  if [ "$rc" -eq "$MODEL_NOT_DOWNLOADED" ]; then
    confirm "Download the model now (size shown above), then check it?" y || { say "Nothing was changed."; return 0; }
    run_py clipcheck --model "$model" --download --no-cpu-compare ${DB_ARGS[@]+"${DB_ARGS[@]}"}
    rc=$?
    if [ "$rc" -eq 130 ]; then warn "Download stopped. Choose this option again to fetch the rest."; return 130; fi
    if [ "$rc" -ne 0 ]; then fail "The model could not be downloaded or did not pass its check (see above). Nothing was changed."; return 1; fi
  elif [ "$rc" -eq 2 ]; then
    fail "The preview could not run (see above): is there an index yet (choose 2 or 3), and is the model name right?"; return 1
  elif [ "$rc" -ne 0 ]; then
    fail "The preview could not run (see above). Nothing was changed."; return 1
  fi
  conf_set SEARCH_MODEL "$model"
  confirm "Process the photos now?" y || { say "Nothing was changed."; return 0; }
  run_py embed --apply --model "$model" ${DB_ARGS[@]+"${DB_ARGS[@]}"}
  rc=$?
  case "$rc" in
    0)   ok "Done. Searching by description in the web interface is the next step being built." ;;
    130) warn "Stopped. Run this option again to continue where it left off." ;;
    1)   warn "Some photos could not be processed (see above). The rest were saved; they are tried again next time." ;;
    *)   fail "Preparing search did not finish (exit code $rc)." ;;
  esac
  return "$rc"
}

# ---------------------------------------------------------------- 6. check a search model
action_clipcheck() {
  header "Check a search model"
  say "Checks that a search model works on this computer and times it, using generated test pictures only (never your photos)."
  say "A model is only downloaded after you agree."
  ensure_modules "Search" requirements-search.txt tokenizers || return 1
  local model default rc
  run_py clipcheck --list
  default="$(conf_get SEARCH_MODEL)"; default="${default:-$DEFAULT_SEARCH_MODEL}"
  printf 'Model to check (Enter = %s; or a name from the list): ' "$default"
  IFS= read -r model || model=""
  model="$(clean_path "$model")"; model="${model:-$default}"
  run_py clipcheck --model "$model" ${DB_ARGS[@]+"${DB_ARGS[@]}"}
  rc=$?
  if [ "$rc" -eq "$MODEL_NOT_DOWNLOADED" ]; then
    confirm "Download the model now (size shown above), then check it?" y || { say "Nothing was downloaded."; return 0; }
    run_py clipcheck --model "$model" --download ${DB_ARGS[@]+"${DB_ARGS[@]}"}
    rc=$?
  fi
  case "$rc" in
    0)   conf_set SEARCH_MODEL "$model"; ok "The model works. Option 5 will now offer it by default." ;;
    130) warn "Stopped. Choose this option again; a download continues where it stopped." ;;
    1)   fail "The model did not pass the check (see above)." ;;
    *)   fail "The check could not run (see above): is the model name right?" ;;
  esac
  return "$rc"
}

# ---------------------------------------------------------------- menu
menu() {
  local choice
  while true; do
    printf '\n%sPhoto sorter%s\n' "$BOLD" "$RESET"
    echo " 1) Start the web interface"
    echo " 2) Intake and sort photos"
    echo " 3) Find faces and group people (refresh)"
    echo " 4) Read camera details"
    echo " 5) Prepare search (search vectors)"
    echo " 6) Check a search model"
    echo " q) Quit"
    printf 'Choose: '
    IFS= read -r choice || { echo; return 0; }
    case "$(printf '%s' "$choice" | tr '[:upper:]' '[:lower:]')" in
      1|ui)    action_ui;    pause ;;
      2|sort)  action_sort;  pause ;;
      3|faces) action_faces; pause ;;
      4|metadata) action_metadata; pause ;;
      5|search) action_search; pause ;;
      6|clipcheck) action_clipcheck; pause ;;
      q|quit|exit) return 0 ;;
      "") ;;
      *) warn "Please type 1, 2, 3, 4, 5, 6 or q." ;;
    esac
  done
}

case "${1:-}" in
  -h|--help|help) usage; exit 0 ;;
esac

require_python
trap ':' INT   # Ctrl+C stops the running program, then returns to the menu
case "${1:-}" in
  1|ui)    action_ui;    exit $? ;;
  2|sort)  action_sort;  exit $? ;;
  3|faces) action_faces; exit $? ;;
  4|metadata) action_metadata; exit $? ;;
  5|search) action_search; exit $? ;;
  6|clipcheck) action_clipcheck; exit $? ;;
  "")      menu ;;
  *)       fail "Unknown option: $1"; usage; exit 2 ;;
esac
