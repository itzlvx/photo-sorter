"""Deploy (release) Photo sorter to the production location (Windows-friendly twin of deploy.sh).

  deploy.bat --target D:\\apps\\photo-sorter     deploy there
  deploy.bat --dry-run                          show what would be copied, change nothing
  deploy.bat --rollback                         put back the previous version
Options: --skip-tests  --no-backup  --yes (no questions)  --keep N (backups to keep, default 5)
The target can also be set with DEPLOY_TARGET=... in deploy.conf (copy deploy.conf.example) or in the environment.
Deploying copies only the program files. It never touches the index database, thumbnails, .photo-sorter.conf
or a virtual environment that already exist in the production folder.
"""
import filecmp
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from _cli import APP_DIR, clean_path, conf_read, confirm, fail, header, ok, say, warn

PLACEHOLDER = "/path/to/production/photo-sorter"
MANIFEST = ".deployed-files"
NOT_DEPLOYED = {"deploy.py", "run_tests.py"}


def collect_files():
    files = []
    files += [p.name for p in APP_DIR.glob("*.py")]
    files += [p.name for p in APP_DIR.glob("requirements*.txt") if p.name != "requirements-dev.txt"]
    files += [n for n in ("README.md", "photo-sorter.sh", "photo-sorter.bat") if (APP_DIR / n).is_file()]
    for folder in ("web", "docs", "scripts"):
        base = APP_DIR / folder
        if base.is_dir():
            for p in base.rglob("*"):
                if p.is_file() and p.suffix != ".pyc" and "__pycache__" not in p.parts and p.name != ".DS_Store" \
                        and not (folder == "scripts" and p.name in NOT_DEPLOYED):
                    files.append(p.relative_to(APP_DIR).as_posix())
    return sorted(set(files))


def describe_version():
    try:
        out = subprocess.run(["git", "-C", str(APP_DIR), "describe", "--tags", "--always", "--dirty"], capture_output=True, text=True)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except OSError:
        pass
    return datetime.now().strftime("%Y-%m-%d")


def backups_in(folder):
    return sorted(p for p in folder.iterdir() if p.is_dir()) if folder.is_dir() else []


def parse(argv):
    opts = dict(target="", dry=False, rollback=False, skip_tests=False, no_backup=False, yes=False, keep="")
    args = list(argv)
    while args:
        a = args.pop(0)
        if a in ("-h", "--help"):
            print(__doc__.strip())
            raise SystemExit(0)
        elif a == "--target":
            opts["target"] = args.pop(0) if args else ""
        elif a == "--dry-run":
            opts["dry"] = True
        elif a == "--rollback":
            opts["rollback"] = True
        elif a == "--skip-tests":
            opts["skip_tests"] = True
        elif a == "--no-backup":
            opts["no_backup"] = True
        elif a in ("--yes", "-y"):
            opts["yes"] = True
        elif a == "--keep":
            opts["keep"] = args.pop(0) if args else ""
        else:
            fail(f"Unknown option: {a} (try --help)")
            raise SystemExit(2)
    return opts


def main(argv):
    o = parse(argv)
    conf = Path(os.environ.get("DEPLOY_CONF") or APP_DIR / "deploy.conf")
    target_text = o["target"] or os.environ.get("DEPLOY_TARGET", "") or conf_read(conf, "DEPLOY_TARGET")
    keep_text = o["keep"] or os.environ.get("KEEP_BACKUPS", "") or conf_read(conf, "KEEP_BACKUPS") or "5"
    if not keep_text.isdigit():
        fail("--keep must be a number.")
        return 1
    keep = int(keep_text)
    target_text = clean_path(target_text)
    if not target_text or "/path/to/" in target_text.replace("\\", "/") or ("<" in target_text and ">" in target_text):
        fail(f"No production location is set yet (it is still the placeholder: {target_text or 'empty'}).")
        say("Pass it once:        deploy.bat --target D:\\where\\it\\goes")
        say("or save it:          copy deploy.conf.example to deploy.conf and edit DEPLOY_TARGET")
        return 2
    target = Path(target_text)
    if target == Path(target.anchor):
        fail("The target can't be the root folder.")
        return 1
    backups = target.parent / (target.name + "-backups")

    if o["rollback"]:
        header("Rollback")
        found = backups_in(backups)
        if not found:
            fail(f"There is no backup in {backups} to go back to.")
            return 1
        latest = found[-1]
        say(f"Restoring {latest} into {target}")
        if not o["yes"] and not confirm("Put the previous version back?", False):
            say("Nothing was changed.")
            return 0
        if o["dry"]:
            say("Dry run: nothing was changed.")
            return 0
        manifest = target / MANIFEST
        if manifest.is_file():
            for rel in manifest.read_text(encoding="utf-8").splitlines():
                if rel.strip():
                    (target / rel).unlink(missing_ok=True)
        shutil.copytree(latest, target, dirs_exist_ok=True)
        ok(f"Rolled back to {latest.name}. The backup folder was kept.")
        return 0

    if o["skip_tests"]:
        warn("Skipping the tests (--skip-tests).")
    elif o["dry"]:
        say("Dry run: the tests would run first (run-tests.bat).")
    else:
        header("Running the tests first")
        if subprocess.call([sys.executable, str(APP_DIR / "scripts" / "run_tests.py")], cwd=str(APP_DIR)) != 0:
            fail("The tests failed, so nothing was deployed. Fix them, or use --skip-tests if you really must.")
            return 1

    header("Deploy")
    files = collect_files()
    if not files:
        fail("Found no program files to deploy (is this the right folder?).")
        return 1
    version = describe_version()
    say(f"From:    {APP_DIR}")
    say(f"To:      {target}")
    say(f"Version: {version} ({len(files)} files)")
    if o["dry"]:
        say("")
        for f in files:
            say("  " + f)
        say("")
        say("Dry run: nothing was copied.")
        return 0
    if not o["yes"] and not confirm("Deploy now?", False):
        say("Nothing was changed.")
        return 0

    new_manifest = sorted(set(files) | {"VERSION", MANIFEST})
    old_manifest = []
    if (target / MANIFEST).is_file():
        old_manifest = [l.strip() for l in (target / MANIFEST).read_text(encoding="utf-8").splitlines() if l.strip()]
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        fail(f"Could not create {target}: {error}")
        return 1

    if o["no_backup"]:
        warn("No backup (--no-backup).")
    else:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        made = False
        for rel in sorted(set(new_manifest) | set(old_manifest)):
            src = target / rel
            if src.is_file():
                dst = backups / stamp / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                made = True
        if made:
            say(f"Backed up the current files to {backups / stamp}")
            old = backups_in(backups)
            for folder in old[: max(0, len(old) - keep)]:
                shutil.rmtree(folder, ignore_errors=True)
        else:
            say("Nothing to back up (first deployment here).")

    removed = 0
    for rel in old_manifest:
        if rel not in new_manifest and (target / rel).is_file():
            (target / rel).unlink()
            removed += 1
    try:
        for rel in files:
            dst = target / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(APP_DIR / rel, dst)
        (target / "VERSION").write_text(f"version={version}\ndeployed_at={datetime.now():%Y-%m-%d %H:%M:%S}\nfrom={APP_DIR}\n", encoding="utf-8", newline="\n")
        (target / MANIFEST).write_text("\n".join(new_manifest) + "\n", encoding="utf-8", newline="\n")
    except OSError as error:
        fail(f"Copying failed part-way ({error}). Run deploy.bat --target \"{target}\" --rollback to go back.")
        return 1

    header("Checking the deployment")
    bad = 0
    for rel in files:
        if not (target / rel).is_file() or not filecmp.cmp(APP_DIR / rel, target / rel, shallow=False):
            fail(f"  differs or missing: {rel}")
            bad = 1
    if not bad:
        ok("  every file matches")
    started = subprocess.run([sys.executable, "sort_photos.py", "--help"], cwd=str(target), capture_output=True,
                             env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
    if started.returncode == 0:
        ok("  sort_photos.py starts")
    else:
        fail(f"  sort_photos.py did not start in {target} (is Pillow installed for that Python?)")
        bad = 1
    if not bad:
        ok(f"Deployed {version} to {target}" + (f" ({removed} old file(s) removed)." if removed else "."))
        say(f'Go back with: deploy.bat --target "{target}" --rollback')
        return 0
    fail(f'The deployment has problems. Go back with: deploy.bat --target "{target}" --rollback')
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        sys.exit(130)
