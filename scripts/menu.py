"""Photo sorter menu (Windows-friendly twin of photo-sorter.sh).

  photo-sorter.bat          show the menu
  photo-sorter.bat ui       start the web interface straight away   (also: 1)
  photo-sorter.bat sort     intake and sort photos                  (also: 2)
  photo-sorter.bat faces    refresh, find faces, group people       (also: 3)
  photo-sorter.bat metadata read camera details for indexed photos (also: 4)

Settings (all optional): PHOTO_SORTER_PYTHON, PHOTO_SORTER_DB, PHOTO_SORTER_UI_ARGS, PHOTO_SORTER_FACES_ARGS.
"""
import importlib.util
import os
import shlex
import subprocess
import sys

from _cli import (APP_DIR, ask, ask_dir, clean_path, conf_get, conf_set, confirm, fail, header, ok, run, run_py, say, warn)

DB_ARGS = ["--db", os.environ["PHOTO_SORTER_DB"]] if os.environ.get("PHOTO_SORTER_DB") else []


def extra(name):
    return shlex.split(os.environ.get(name, ""), posix=False)


def ensure_modules(label, requirements, *modules):
    missing = [m for m in modules if importlib.util.find_spec(m) is None]
    if not missing:
        return True
    warn(f"{label} needs Python packages that are not installed: {' '.join(missing)}")
    if not confirm(f"Install them now with pip ({requirements})?", True):
        return False
    if subprocess.call([sys.executable, "-m", "pip", "install", "-r", requirements], cwd=str(APP_DIR)) != 0:
        fail("pip could not install them.")
        return False
    if requirements == "requirements-faces.txt":
        say("Using an NVIDIA GPU? Follow 'Using your NVIDIA GPU' in README.md afterwards.")
    return True


def action_ui():
    header("Start the web interface")
    if not ensure_modules("The web interface", "requirements-ui.txt", "fastapi", "uvicorn"):
        return 1
    say("It opens in your browser, normally at http://127.0.0.1:8765. Press Ctrl+C here to stop it.")
    return run_py("ui", *DB_ARGS, *extra("PHOTO_SORTER_UI_ARGS"))


def action_sort():
    header("Intake and sort photos")
    source = ask_dir("Where are the photos to sort? (searched recursively, q to cancel)", conf_get("LAST_SOURCE"), True)
    if source is None:
        return 0
    dest = ask_dir("Where should the sorted library be? (Year\\01-jan style folders go here, q to cancel)", conf_get("LIBRARY_DIR"), False)
    if dest is None:
        return 0
    say("Copy keeps your originals untouched. Move deletes each original only after the copy is checked.")
    move = ["--move"] if confirm("Move the photos instead of copying them?", False) else []
    conf_set("LAST_SOURCE", source)
    conf_set("LIBRARY_DIR", dest)

    say("First a preview. Nothing is changed yet.")
    rc = run_py("sort", source, dest, *move, *DB_ARGS)
    if rc >= 2:
        fail("The preview could not run (see above). Nothing was changed.")
        return 1
    if rc == 1:
        warn("The preview reported some errors (see above).")
    if not confirm("Sort the photos like this?", False):
        say("Nothing was changed.")
        return 0
    rc = run_py("sort", source, dest, *move, *DB_ARGS, "--apply")
    if rc == 0:
        ok("Sorting finished.")
    else:
        warn(f"Sorting finished with problems (exit code {rc}). Read the messages above.")
    say("Next: choose 3 to find the faces in the new photos.")
    return rc


def action_faces():
    header("Find faces and group people")
    say("This runs three steps: refresh the index from your sorted library, scan new photos for faces, group faces into people.")
    say("Your photos are only read, never changed.")
    lib = ask_dir("Sorted library folder (q to cancel)", conf_get("LIBRARY_DIR"), True)
    if lib is None:
        return 0
    conf_set("LIBRARY_DIR", lib)

    limit = clean_path(ask("How many photos should be scanned for faces? Enter = all new photos, or a number for a quick test: ") or "")
    limit_args = []
    if limit:
        if limit.isdigit() and int(limit) > 0:
            limit_args = ["--limit", str(int(limit))]
        else:
            warn("That is not a number, so all new photos will be scanned.")
    hardware = (ask("Hardware: [a]uto (GPU if set up, else CPU), [g]pu only, [c]pu only. Enter = auto: ") or "").strip().lower()
    hw_args = ["--gpu"] if hardware in ("g", "gpu") else ["--cpu"] if hardware in ("c", "cpu") else []
    if not confirm("Start now?", True):
        say("Nothing was changed.")
        return 0
    if not ensure_modules("Face scanning", "requirements-faces.txt", "insightface", "onnxruntime"):
        return 1

    say("Step 1 of 3: refresh the index")
    rc = run_py("refresh", lib, "--apply", *DB_ARGS)
    if rc >= 2:
        fail("The refresh could not run. Stopping.")
        return 1
    if rc == 1 and not confirm("The refresh reported some errors. Continue anyway?", False):
        return 1

    say("Step 2 of 3: scan for faces (progress is saved, so you can stop with Ctrl+C and run this again)")
    rc = run_py("faces", "--apply", *limit_args, *hw_args, *DB_ARGS, *extra("PHOTO_SORTER_FACES_ARGS"))
    if rc == 130:
        warn("Stopped. Run this option again to continue where it left off.")
        return 130
    if rc >= 2:
        fail("Face scanning could not run. Stopping.")
        return 1
    if rc == 1:
        warn("Some photos could not be scanned (see above). Going on with the rest.")

    say("Step 3 of 3: group faces into people")
    rc = run_py("cluster", "--apply", *DB_ARGS)
    if rc != 0:
        fail(f"Grouping did not finish (exit code {rc}).")
        return rc
    ok("Done. Choose 1 to open the web interface and name the people.")
    return 0


def action_metadata():
    header("Read camera details")
    say("Reads camera, lens, exposure, size and GPS position for photos already in the index. Your photos are only read, never changed.")
    say("New photos get these automatically when they are sorted or refreshed; this is for catching up or reading everything again.")
    reread = ["--reread"] if confirm("Read every photo again, not only the ones without details?", False) else []
    say("First a preview. Nothing is changed yet.")
    rc = run_py("metadata", *reread, *DB_ARGS)
    if rc == 2:
        fail("There is no index yet. Choose 2 to sort photos or 3 to refresh your library first.")
        return 1
    if rc != 0:
        fail("The preview could not run (see above). Nothing was changed.")
        return 1
    if not confirm("Read them now?", True):
        say("Nothing was changed.")
        return 0
    rc = run_py("metadata", "--apply", *reread, *DB_ARGS)
    if rc == 0:
        ok("Done. The details show in the photo viewer of the web interface.")
    elif rc == 130:
        warn("Stopped. Run this option again to continue where it left off.")
    elif rc == 1:
        warn("Some photos could not be read (see above). The rest were saved.")
    else:
        fail(f"Reading camera details did not finish (exit code {rc}).")
    return rc


ACTIONS = {"1": action_ui, "ui": action_ui, "2": action_sort, "sort": action_sort, "3": action_faces, "faces": action_faces,
           "4": action_metadata, "metadata": action_metadata}


def menu():
    while True:
        print("\nPhoto sorter\n 1) Start the web interface\n 2) Intake and sort photos\n 3) Find faces and group people (refresh)\n 4) Read camera details\n q) Quit")
        choice = ask("Choose: ")
        if choice is None:
            return 0
        choice = choice.strip().lower()
        if choice in ACTIONS:
            try:
                ACTIONS[choice]()
            except KeyboardInterrupt:
                print()
            ask("\nPress Enter to continue ... ")
        elif choice in ("q", "quit", "exit"):
            return 0
        elif choice:
            warn("Please type 1, 2, 3, 4 or q.")


def main(argv):
    if argv and argv[0] in ("-h", "--help", "help"):
        print(__doc__.strip())
        return 0
    if not argv:
        return menu()
    if argv[0] in ACTIONS:
        try:
            return ACTIONS[argv[0]]()
        except KeyboardInterrupt:
            return 130
    fail(f"Unknown option: {argv[0]}")
    print(__doc__.strip())
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
