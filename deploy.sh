#!/usr/bin/env bash
# Deploy (release) Photo sorter to the production location.
#   ./deploy.sh --target /path/to/production/photo-sorter     deploy there
#   ./deploy.sh --dry-run                                     show what would be copied, change nothing
#   ./deploy.sh --rollback                                    put back the previous version
# Options: --skip-tests  --no-backup  --yes (no questions)  --keep N (backups to keep, default 5)
# The target can also be set with DEPLOY_TARGET=... in deploy.conf (copy deploy.conf.example) or in the environment.
# Deploying copies only the program files. It never touches the index database, thumbnails, .photo-sorter.conf
# or a virtual environment that already exist in the production folder.

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/common.sh
. "$APP_DIR/scripts/common.sh"

DEPLOY_CONF="${DEPLOY_CONF:-$APP_DIR/deploy.conf}"
PLACEHOLDER="/path/to/production/photo-sorter"
TARGET=""; DRY=0; SKIP_TESTS=0; NO_BACKUP=0; ROLLBACK=0; YES=0; KEEP=""

while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help) sed -n '2,9p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    --target) shift; TARGET="${1:-}" ;;
    --dry-run) DRY=1 ;;
    --rollback) ROLLBACK=1 ;;
    --skip-tests) SKIP_TESTS=1 ;;
    --no-backup) NO_BACKUP=1 ;;
    --yes|-y) YES=1 ;;
    --keep) shift; KEEP="${1:-}" ;;
    *) fail "Unknown option: $1 (try --help)"; exit 2 ;;
  esac
  shift
done

[ -n "$TARGET" ] || TARGET="${DEPLOY_TARGET:-}"
[ -n "$TARGET" ] || TARGET="$(conf_read "$DEPLOY_CONF" DEPLOY_TARGET)"
[ -n "$KEEP" ]   || KEEP="${KEEP_BACKUPS:-$(conf_read "$DEPLOY_CONF" KEEP_BACKUPS)}"
[ -n "$KEEP" ]   || KEEP=5
case "$KEEP" in *[!0-9]*|"") die "--keep must be a number." ;; esac

TARGET="$(clean_path "$TARGET")"
case "$TARGET" in
  ""|*"/path/to/"*|*"<"*">"*|*PLACEHOLDER*)
    fail "No production location is set yet (it is still the placeholder: ${TARGET:-empty})."
    say  "Pass it once:        ./deploy.sh --target /where/it/goes"
    say  "or save it:          copy deploy.conf.example to deploy.conf and edit DEPLOY_TARGET"
    exit 2 ;;
esac
TARGET="$(to_unix "$TARGET")"
TARGET="${TARGET%/}"
[ -n "$TARGET" ] || die "The target can't be the root folder."
BACKUPS="${TARGET}-backups"
MANIFEST=".deployed-files"

# ---------------------------------------------------------------- helpers
collect_files() {    # the program files, one relative path per line
  (
    cd "$APP_DIR" || exit 1
    for f in *.py; do [ -f "$f" ] && echo "$f"; done
    for f in requirements*.txt; do
      case "$f" in requirements-dev.txt) ;; *) [ -f "$f" ] && echo "$f" ;; esac
    done
    for f in README.md photo-sorter.sh photo-sorter.bat; do [ -f "$f" ] && echo "$f"; done
    for d in web docs scripts; do
      [ -d "$d" ] && find "$d" -type f ! -name '*.pyc' ! -path '*/__pycache__/*' ! -name '.DS_Store' ! -name deploy.py ! -name run_tests.py
    done
  ) | LC_ALL=C sort
}

describe_version() {
  if command -v git >/dev/null 2>&1 && git -C "$APP_DIR" rev-parse --git-dir >/dev/null 2>&1; then
    git -C "$APP_DIR" describe --tags --always --dirty 2>/dev/null && return
  fi
  date +%Y-%m-%d
}

latest_backup() { [ -d "$BACKUPS" ] && ls -1d "$BACKUPS"/*/ 2>/dev/null | LC_ALL=C sort | tail -n 1 | sed 's:/$::'; }

# ---------------------------------------------------------------- rollback
if [ "$ROLLBACK" = 1 ]; then
  header "Rollback"
  backup="$(latest_backup)"
  [ -n "$backup" ] || die "There is no backup in $BACKUPS to go back to."
  say "Restoring $backup into $TARGET"
  if [ "$YES" != 1 ] && ! confirm "Put the previous version back?" n; then say "Nothing was changed."; exit 0; fi
  if [ "$DRY" = 1 ]; then say "Dry run: nothing was changed."; exit 0; fi
  if [ -f "$TARGET/$MANIFEST" ]; then
    while IFS= read -r rel; do [ -n "$rel" ] && rm -f "$TARGET/$rel"; done <"$TARGET/$MANIFEST"
  fi
  cp -a "$backup"/. "$TARGET"/ || die "Could not copy the backup back."
  ok "Rolled back to $(basename "$backup"). The backup folder was kept."
  exit 0
fi

# ---------------------------------------------------------------- 1. tests
if [ "$SKIP_TESTS" = 1 ]; then
  warn "Skipping the tests (--skip-tests)."
elif [ "$DRY" = 1 ]; then
  say "Dry run: the tests would run first (./run-tests.sh)."
else
  header "Running the tests first"
  bash "$APP_DIR/run-tests.sh" || die "The tests failed, so nothing was deployed. Fix them, or use --skip-tests if you really must."
fi

# ---------------------------------------------------------------- 2. what gets copied
header "Deploy"
FILES="$(collect_files)"
[ -n "$FILES" ] || die "Found no program files to deploy (is this the right folder?)."
count="$(printf '%s\n' "$FILES" | wc -l | tr -d ' ')"
VERSION_TEXT="$(describe_version)"
say "From:    $APP_DIR"
say "To:      $TARGET"
say "Version: $VERSION_TEXT ($count files)"
if [ "$DRY" = 1 ]; then
  say ""; printf '%s\n' "$FILES" | sed 's/^/  /'
  say ""; say "Dry run: nothing was copied."
  exit 0
fi
if [ "$YES" != 1 ] && ! confirm "Deploy now?" n; then say "Nothing was changed."; exit 0; fi

STAGE="$(mktemp -d)" || die "Could not create a temporary folder."
trap 'rm -rf "$STAGE"' EXIT
printf '%s\n' "$FILES" >"$STAGE/.list"
mkdir -p "$STAGE/files"
tar -cf - -C "$APP_DIR" -T "$STAGE/.list" | tar -xf - -C "$STAGE/files" || die "Could not prepare the files."
{ echo "version=$VERSION_TEXT"; echo "deployed_at=$(date '+%Y-%m-%d %H:%M:%S')"; echo "from=$APP_DIR"; } >"$STAGE/files/VERSION"
{ cat "$STAGE/.list"; echo VERSION; echo "$MANIFEST"; } | LC_ALL=C sort >"$STAGE/files/$MANIFEST"

mkdir -p "$TARGET" || die "Could not create $TARGET."
[ -w "$TARGET" ] || die "No permission to write to $TARGET."

# ---------------------------------------------------------------- 3. back up what will change
OLD_LIST=""
[ -f "$TARGET/$MANIFEST" ] && OLD_LIST="$(cat "$TARGET/$MANIFEST")"
if [ "$NO_BACKUP" = 1 ]; then
  warn "No backup (--no-backup)."
else
  stamp="$(date +%Y%m%d-%H%M%S)"
  made_backup=0
  while IFS= read -r rel; do
    [ -n "$rel" ] || continue
    if [ -f "$TARGET/$rel" ]; then
      mkdir -p "$BACKUPS/$stamp/$(dirname "$rel")" || die "Could not create the backup folder $BACKUPS."
      cp -a "$TARGET/$rel" "$BACKUPS/$stamp/$rel" || die "Could not back up $rel."
      made_backup=1
    fi
  done < <({ cat "$STAGE/files/$MANIFEST"; printf '%s\n' "$OLD_LIST"; } | LC_ALL=C sort -u)
  if [ "$made_backup" = 1 ]; then
    say "Backed up the current files to $BACKUPS/$stamp"
    # keep only the newest $KEEP backups
    all_backups="$(ls -1d "$BACKUPS"/*/ 2>/dev/null | LC_ALL=C sort | sed 's:/$::')"
    total="$(printf '%s\n' "$all_backups" | grep -c .)"
    if [ "$total" -gt "$KEEP" ]; then
      printf '%s\n' "$all_backups" | head -n "$((total - KEEP))" | while IFS= read -r old; do
        [ -n "$old" ] && rm -rf "$old"
      done
    fi
  else
    say "Nothing to back up (first deployment here)."
  fi
fi

# ---------------------------------------------------------------- 4. copy, and remove files that are no longer part of the program
removed=0
while IFS= read -r rel; do
  [ -n "$rel" ] || continue
  if ! grep -qxF -- "$rel" "$STAGE/files/$MANIFEST" && [ -f "$TARGET/$rel" ]; then rm -f "$TARGET/$rel"; removed=$((removed + 1)); fi
done <<<"$OLD_LIST"
tar -cf - -C "$STAGE/files" . | tar -xf - -C "$TARGET" || die "Copying failed part-way. Run ./deploy.sh --rollback to go back."
chmod +x "$TARGET/photo-sorter.sh" 2>/dev/null || true

# ---------------------------------------------------------------- 5. check what arrived
header "Checking the deployment"
bad=0
while IFS= read -r rel; do
  [ -n "$rel" ] || continue
  [ "$rel" = "$MANIFEST" ] && continue
  if [ ! -f "$TARGET/$rel" ] || ! cmp -s "$STAGE/files/$rel" "$TARGET/$rel"; then fail "  differs or missing: $rel"; bad=1; fi
done <"$STAGE/files/$MANIFEST"
[ "$bad" = 0 ] && ok "  every file matches"

if find_python; then
  if ( cd "$TARGET" && PYTHONDONTWRITEBYTECODE=1 "${PY[@]}" sort_photos.py --help >/dev/null 2>&1 ); then
    ok "  sort_photos.py starts"
  else
    fail "  sort_photos.py did not start in $TARGET (is Pillow installed for that Python?)"; bad=1
  fi
else
  warn "  Python was not found here, so the start-up check was skipped."
fi

if [ "$bad" = 0 ]; then
  note=""; [ "$removed" -gt 0 ] && note=" ($removed old file(s) removed)"
  ok "Deployed $VERSION_TEXT to $TARGET$note."
  say "Go back with: ./deploy.sh --target $TARGET --rollback"
  exit 0
fi
fail "The deployment has problems. Go back with: ./deploy.sh --target $TARGET --rollback"
exit 1
