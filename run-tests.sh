#!/usr/bin/env bash
# Run the Photo sorter tests.
#   ./run-tests.sh               quick checks + all tests except the browser ones (about a minute)
#   ./run-tests.sh --all         also run the browser tests (needs Playwright, see --install)
#   ./run-tests.sh --e2e         only the browser tests
#   ./run-tests.sh --cov         show which lines of the code the tests did not reach
#   ./run-tests.sh --install     first install the test tools (pip install -r requirements-dev.txt)
#   ./run-tests.sh -k rename     only tests whose name contains "rename"   (also: -x stops at the first failure, -v is chatty)
#   ./run-tests.sh --no-static   skip the syntax checks of the .py, .sh and .js files
# Anything after -- goes straight to pytest.  Exit code 0 means everything passed.

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/common.sh
. "$APP_DIR/scripts/common.sh"

MODE=fast; COV=0; INSTALL=0; STATIC=1
PYTEST_ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help) sed -n '2,10p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    --fast) MODE=fast ;;
    --all) MODE=all ;;
    --e2e) MODE=e2e ;;
    --cov) COV=1 ;;
    --install) INSTALL=1 ;;
    --no-static) STATIC=0 ;;
    -k) shift; PYTEST_ARGS+=(-k "${1:-}") ;;
    -x|--failfast) PYTEST_ARGS+=(-x) ;;
    -v|--verbose) PYTEST_ARGS+=(-v) ;;
    --) shift; PYTEST_ARGS+=("$@"); break ;;
    *) fail "Unknown option: $1 (try --help)"; exit 2 ;;
  esac
  shift
done

require_python
cd "$APP_DIR" || exit 2

if [ "$INSTALL" = 1 ]; then
  header "Installing the test tools"
  "${PY[@]}" -m pip install -r requirements-dev.txt || die "pip could not install the test tools."
  if [ "$MODE" != fast ]; then
    say "Browser tests also need a browser: installing Chromium for Playwright ..."
    "${PY[@]}" -m playwright install chromium || warn "Could not install Chromium; the browser tests will be skipped."
  fi
fi

problems=0
if [ "$STATIC" = 1 ]; then
  header "Syntax checks"
  # Python: parse every file without writing .pyc files
  if "${PY[@]}" - <<'PYCHECK'
import ast, pathlib, sys
bad = 0
for path in sorted(pathlib.Path(".").rglob("*.py")):
    if any(part in {".venv", "venv", "__pycache__", ".git", "node_modules"} for part in path.parts):
        continue
    try:
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as error:
        bad += 1
        print(f"  {path}: {error}")
sys.exit(1 if bad else 0)
PYCHECK
  then ok "  Python files: ok"; else fail "  Python files: syntax errors"; problems=1; fi
  # Shell scripts
  shell_bad=0
  for f in ./*.sh scripts/*.sh; do
    [ -f "$f" ] || continue
    bash -n "$f" || { fail "  $f: syntax error"; shell_bad=1; }
  done
  if [ "$shell_bad" = 0 ]; then ok "  Shell scripts: ok"; else problems=1; fi
  # The web page script
  if command -v node >/dev/null 2>&1; then
    if node --check web/app.js; then ok "  web/app.js: ok"; else fail "  web/app.js: syntax error"; problems=1; fi
  else
    warn "  web/app.js not checked (Node.js is not installed; that is fine)"
  fi
  [ "$problems" = 0 ] || die "Fix the syntax errors above first."
fi

if ! "${PY[@]}" -c "import pytest" >/dev/null 2>&1; then
  die "pytest is not installed for this Python. Run: ./run-tests.sh --install"
fi

case "$MODE" in
  fast) PYTEST_ARGS=(-m "not e2e" "${PYTEST_ARGS[@]}") ;;
  e2e)  PYTEST_ARGS=(-m e2e "${PYTEST_ARGS[@]}") ;;
esac
if [ "$COV" = 1 ]; then
  if "${PY[@]}" -c "import pytest_cov" >/dev/null 2>&1; then
    PYTEST_ARGS+=(--cov=. --cov-report=term-missing --cov-config=.coveragerc)
  else
    warn "pytest-cov is not installed, so no coverage report. Run: ./run-tests.sh --install"
  fi
fi

header "Tests ($MODE)"
PYTHONDONTWRITEBYTECODE=1 "${PY[@]}" -m pytest tests ${PYTEST_ARGS[@]+"${PYTEST_ARGS[@]}"}
rc=$?
echo
if [ "$rc" -eq 0 ]; then ok "All tests passed."; else fail "Tests failed (pytest exit code $rc)."; fi
exit "$rc"
