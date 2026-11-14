"""Small helpers shared by menu.py, run_tests.py and deploy.py (the Windows-friendly twins of the .sh scripts)."""
import os
import subprocess
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def _paint(code, text):
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


def say(text=""):
    print(text, flush=True)


def ok(text):
    print(_paint(32, text), flush=True)


def warn(text):
    print(_paint(33, text), file=sys.stderr, flush=True)


def fail(text):
    print(_paint(31, text), file=sys.stderr, flush=True)


def header(text):
    print("\n" + _paint(1, f"== {text} =="), flush=True)


def clean_path(text):
    """Trim spaces and surrounding quotes (drag-and-drop adds them); expand a leading ~."""
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1].strip()
    if text == "~" or text.startswith(("~/", "~\\")):
        text = str(Path.home()) + text[1:]
    return text


def ask(prompt):
    """input() that returns None at end of input."""
    try:
        return input(prompt)
    except EOFError:
        print()
        return None


def ask_dir(question, default="", must_exist=True):
    """Returns the folder, or None when the person typed q or input ended."""
    while True:
        reply = ask(f"{question} [{default}]: " if default else f"{question}: ")
        if reply is None:
            return None
        reply = clean_path(reply)
        if reply.lower() == "q":
            return None
        reply = reply or default
        if not reply:
            warn("Please enter a folder (or q to cancel).")
            continue
        if must_exist and not Path(reply).is_dir():
            warn(f"That folder doesn't exist: {reply}")
            continue
        return reply


def confirm(question, default=False):
    reply = ask(f"{question} {'[Y/n]' if default else '[y/N]'} ")
    if reply is None:
        return False
    reply = reply.strip().lower()
    return default if reply == "" else reply in ("y", "yes")


# ---- remembered answers: plain KEY=value lines, shared with the .sh scripts, never executed
def conf_file():
    return Path(os.environ.get("PHOTO_SORTER_CONF") or APP_DIR / ".photo-sorter.conf")


def conf_read(path, key):
    value = ""
    try:
        for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith(key + "="):
                value = line[len(key) + 1:]
    except OSError:
        pass
    return value


def conf_get(key):
    return conf_read(conf_file(), key)


def conf_set(key, value):
    path = conf_file()
    try:
        lines = [l for l in path.read_text(encoding="utf-8", errors="replace").splitlines() if not l.startswith(key + "=")]
    except OSError:
        lines = []
    lines.append(f"{key}={value}")
    try:
        path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    except OSError as error:
        warn(f"Could not save your answers ({error}).")


def run(cmd, cwd=None):
    """Run a command and return its exit code (130 when stopped with Ctrl+C)."""
    say("\n$ " + subprocess.list2cmdline([str(c) for c in cmd]))
    try:
        return subprocess.call([str(c) for c in cmd], cwd=str(cwd or APP_DIR))
    except KeyboardInterrupt:
        return 130
    except OSError as error:
        fail(f"Could not run it: {error}")
        return 2


def run_py(*args):
    return run([sys.executable, "sort_photos.py", *args])
