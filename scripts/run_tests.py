"""Run the Photo sorter tests (Windows-friendly twin of run-tests.sh).

  run-tests.bat               quick checks + all tests except the browser ones
  run-tests.bat --all         also run the browser tests (needs Playwright, see --install)
  run-tests.bat --e2e         only the browser tests
  run-tests.bat --cov         show which lines of the code the tests did not reach
  run-tests.bat --install     first install the test tools (pip install -r requirements-dev.txt)
  run-tests.bat -k rename     only tests whose name contains "rename"   (also: -x stops at the first failure, -v is chatty)
  run-tests.bat --no-static   skip the syntax checks of the .py, .sh and .js files
Anything after -- goes straight to pytest.  Exit code 0 means everything passed.
"""
import ast
import importlib.util
import shutil
import subprocess
import sys

from _cli import APP_DIR, fail, header, ok, say, warn

SKIP_DIRS = {".venv", "venv", "__pycache__", ".git", "node_modules"}


def static_checks():
    header("Syntax checks")
    problems = 0
    bad = 0
    for path in sorted(APP_DIR.rglob("*.py")):
        if SKIP_DIRS & set(path.relative_to(APP_DIR).parts):
            continue
        try:
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as error:
            bad += 1
            say(f"  {path}: {error}")
    if bad:
        fail("  Python files: syntax errors")
        problems += 1
    else:
        ok("  Python files: ok")
    bash = shutil.which("bash")
    if bash and sys.platform == "win32":
        bash = None      # "bash" on Windows is often WSL, which cannot read C:\ paths; the .sh files are for Git Bash and checked elsewhere
    if bash:
        shell_bad = 0
        for path in sorted(list(APP_DIR.glob("*.sh")) + list((APP_DIR / "scripts").glob("*.sh"))):
            if subprocess.call([bash, "-n", str(path)]) != 0:
                fail(f"  {path.name}: syntax error")
                shell_bad += 1
        if shell_bad:
            problems += 1
        else:
            ok("  Shell scripts: ok")
    node = shutil.which("node")
    if node and (APP_DIR / "web" / "app.js").exists():
        if subprocess.call([node, "--check", str(APP_DIR / "web" / "app.js")]) == 0:
            ok("  web/app.js: ok")
        else:
            fail("  web/app.js: syntax error")
            problems += 1
    else:
        warn("  web/app.js not checked (Node.js is not installed; that is fine)")
    return problems == 0


def has(module):
    return importlib.util.find_spec(module) is not None


def main(argv):
    mode, cov, install, static = "fast", False, False, True
    pytest_args = []
    args = list(argv)
    while args:
        a = args.pop(0)
        if a in ("-h", "--help"):
            print(__doc__.strip())
            return 0
        elif a in ("--fast", "--all", "--e2e"):
            mode = a[2:]
        elif a == "--cov":
            cov = True
        elif a == "--install":
            install = True
        elif a == "--no-static":
            static = False
        elif a == "-k":
            pytest_args += ["-k", args.pop(0) if args else ""]
        elif a in ("-x", "--failfast"):
            pytest_args.append("-x")
        elif a in ("-v", "--verbose"):
            pytest_args.append("-v")
        elif a == "--":
            pytest_args += args
            break
        else:
            fail(f"Unknown option: {a} (try --help)")
            return 2

    if install:
        header("Installing the test tools")
        if subprocess.call([sys.executable, "-m", "pip", "install", "-r", "requirements-dev.txt"], cwd=str(APP_DIR)) != 0:
            fail("pip could not install the test tools.")
            return 1
        if mode != "fast":
            say("Browser tests also need a browser: installing Chromium for Playwright ...")
            if subprocess.call([sys.executable, "-m", "playwright", "install", "chromium"], cwd=str(APP_DIR)) != 0:
                warn("Could not install Chromium; the browser tests will be skipped.")

    if static and not static_checks():
        fail("Fix the syntax errors above first.")
        return 1
    if not has("pytest"):
        fail("pytest is not installed for this Python. Run: run-tests.bat --install")
        return 1
    if mode == "fast":
        pytest_args = ["-m", "not e2e", *pytest_args]
    elif mode == "e2e":
        pytest_args = ["-m", "e2e", *pytest_args]
    if cov:
        if has("pytest_cov"):
            pytest_args += ["--cov=.", "--cov-report=term-missing", "--cov-config=.coveragerc"]
        else:
            warn("pytest-cov is not installed, so no coverage report. Run: run-tests.bat --install")

    header(f"Tests ({mode})")
    env = dict(__import__("os").environ, PYTHONDONTWRITEBYTECODE="1")
    try:
        rc = subprocess.call([sys.executable, "-m", "pytest", "tests", *pytest_args], cwd=str(APP_DIR), env=env)
    except KeyboardInterrupt:
        rc = 130
    print()
    if rc == 0:
        ok("All tests passed.")
    else:
        fail(f"Tests failed (pytest exit code {rc}).")
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
