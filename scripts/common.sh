# shellcheck shell=bash
# Shared helpers for photo-sorter.sh, run-tests.sh and deploy.sh.
# Source this file; do not run it. The caller must set APP_DIR first.

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  BOLD=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RESET=$'\033[0m'
else
  BOLD=""; DIM=""; RED=""; GREEN=""; YELLOW=""; RESET=""
fi

say()  { printf '%s\n' "$*"; }
ok()   { printf '%s%s%s\n' "$GREEN" "$*" "$RESET"; }
warn() { printf '%s%s%s\n' "$YELLOW" "$*" "$RESET" >&2; }
fail() { printf '%s%s%s\n' "$RED" "$*" "$RESET" >&2; }
die()  { fail "$*"; exit 1; }
header() { printf '\n%s== %s ==%s\n' "$BOLD" "$*" "$RESET"; }

# ---------------------------------------------------------------- python
PY=()   # the command that runs Python, for example: py -3
try_python() {   # try_python command [args...]  (sets PY when it is Python 3.9 or newer)
  command -v "$1" >/dev/null 2>&1 || [ -x "$1" ] || return 1
  "$@" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1 || return 1
  PY=("$@")
}

find_python() {
  PY=()
  if [ -n "${PHOTO_SORTER_PYTHON:-}" ]; then
    local -a custom
    read -r -a custom <<<"$PHOTO_SORTER_PYTHON"
    try_python "${custom[@]}" && return 0
    fail "PHOTO_SORTER_PYTHON is '$PHOTO_SORTER_PYTHON', but that is not Python 3.9 or newer."
    return 1
  fi
  local venv sub
  for venv in .venv venv; do
    for sub in Scripts/python.exe bin/python; do
      try_python "$APP_DIR/$venv/$sub" && return 0
    done
  done
  try_python py -3 && return 0      # the Windows launcher
  try_python python3 && return 0
  try_python python && return 0
  return 1
}

require_python() {
  find_python && return 0
  die "Python 3.9 or newer was not found. Install it from python.org (tick 'Add python.exe to PATH'), or set PHOTO_SORTER_PYTHON to its path."
}

# ---------------------------------------------------------------- paths and prompts
clean_path() {   # trim spaces and surrounding quotes (drag-and-drop adds them); expand a leading ~
  local p="$1"
  p="${p#"${p%%[![:space:]]*}"}"
  p="${p%"${p##*[![:space:]]}"}"
  case "$p" in
    '"'*'"') p="${p#\"}"; p="${p%\"}" ;;
    "'"*"'") p="${p#\'}"; p="${p%\'}" ;;
  esac
  case "$p" in
    "~"|"~/"*) p="$HOME${p#"~"}" ;;
  esac
  printf '%s' "$p"
}

to_unix()   { if command -v cygpath >/dev/null 2>&1; then cygpath -u "$1" 2>/dev/null || printf '%s' "$1"; else printf '%s' "$1"; fi; }
to_native() { if command -v cygpath >/dev/null 2>&1; then cygpath -w "$1" 2>/dev/null || printf '%s' "$1"; else printf '%s' "$1"; fi; }

# ask_dir VAR "Question" [default] [must_exist: 1 or 0]
# Returns 0 with the answer in VAR, 1 when input ended, 2 when the person typed q to cancel.
ask_dir() {
  local __var="$1" question="$2" default="${3:-}" must_exist="${4:-1}" reply path
  while true; do
    if [ -n "$default" ]; then printf '%s [%s]: ' "$question" "$default"; else printf '%s: ' "$question"; fi
    IFS= read -r reply || { echo; return 1; }
    reply="$(clean_path "$reply")"
    [ "$reply" = "q" ] && return 2
    [ -z "$reply" ] && reply="$default"
    if [ -z "$reply" ]; then warn "Please enter a folder (or q to cancel)."; continue; fi
    path="$(to_unix "$reply")"
    if [ "$must_exist" = 1 ] && [ ! -d "$path" ]; then warn "That folder doesn't exist: $reply"; continue; fi
    printf -v "$__var" '%s' "$reply"
    return 0
  done
}

confirm() {      # confirm "Question" [y|n default]; returns 0 for yes
  local question="$1" default="${2:-n}" hint reply
  if [ "$default" = y ]; then hint="[Y/n]"; else hint="[y/N]"; fi
  printf '%s %s ' "$question" "$hint"
  IFS= read -r reply || { echo; return 1; }
  reply="$(printf '%s' "$reply" | tr '[:upper:]' '[:lower:]')"
  case "$reply" in
    y|yes) return 0 ;;
    "")    [ "$default" = y ] ;;
    *)     return 1 ;;
  esac
}

pause() { printf '\nPress Enter to continue ... '; IFS= read -r _ || true; }

# ---------------------------------------------------------------- remembered answers (never sourced, only parsed)
CONF_FILE="${PHOTO_SORTER_CONF:-${APP_DIR:-.}/.photo-sorter.conf}"

conf_read() {    # conf_read FILE KEY -> prints the value (last one wins), nothing when missing
  local file="$1" key="$2" line
  [ -f "$file" ] || return 0
  line="$(grep -E "^${key}=" "$file" | tail -n 1)" || true
  printf '%s' "${line#*=}"
}
conf_get() { conf_read "$CONF_FILE" "$1"; }
conf_set() {     # conf_set KEY VALUE
  local key="$1" value="$2" tmp
  tmp="$(mktemp)"
  if [ -f "$CONF_FILE" ]; then grep -v -E "^${key}=" "$CONF_FILE" >"$tmp" || true; fi
  printf '%s=%s\n' "$key" "$value" >>"$tmp"
  cat "$tmp" >"$CONF_FILE"
  rm -f "$tmp"
}

# ---------------------------------------------------------------- running the program
quote_cmd() { local out="" a; for a in "$@"; do out+="$(printf '%q ' "$a")"; done; printf '%s' "${out% }"; }

run_py() {       # run_py ARGS... : runs sort_photos.py from the program folder and shows the command
  local -a cmd=("${PY[@]}" sort_photos.py "$@")
  printf '\n%s$ %s%s\n' "$DIM" "$(quote_cmd "${cmd[@]}")" "$RESET"
  ( cd "$APP_DIR" && "${cmd[@]}" )
}
