"""The Windows-friendly twins of the bash scripts: scripts/menu.py, run_tests.py and deploy.py (started by the .bat files).

They run for real with this Python, in a private copy of the program folder whose sort_photos.py is replaced by a
recorder, so nothing is sorted, scanned or installed. The .bat launchers themselves can only be run on Windows;
here they are checked for the things that commonly go wrong (line endings, what they call).
"""
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PLACEHOLDER = "/path/to/production/photo-sorter"

RECORDER = textwrap.dedent('''\
    import os, sys
    with open(os.environ["FAKE_LOG"], "a", encoding="utf-8") as f:
        f.write(" ".join(sys.argv[1:]) + "\\n")
    cmd = sys.argv[1].replace("-", "_") if len(sys.argv) > 1 else ""
    var = "FAKE_RC_" + cmd + ("_apply" if "--apply" in sys.argv else "_download" if "--download" in sys.argv else "")
    sys.exit(int(os.environ.get(var, "0")))
''')

TESTS = textwrap.dedent('''\
    import pytest
    def test_plain(): pass
    @pytest.mark.e2e
    def test_browser(): assert False
''')


class Box:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.app = tmp / "app"
        shutil.copytree(ROOT, self.app, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", ".git", ".venv", "venv", "*.db", ".coverage", "deploy.conf", ".photo-sorter.conf", "tests"))
        (self.app / "sort_photos.py").write_text(RECORDER, encoding="utf-8")
        (self.app / "tests").mkdir()
        (self.app / "tests" / "test_x.py").write_text(TESTS, encoding="utf-8")
        self.log = tmp / "calls.log"
        self.conf = tmp / "answers.conf"
        self.env = {**os.environ, "FAKE_LOG": str(self.log), "PHOTO_SORTER_CONF": str(self.conf), "NO_COLOR": "1", "PYTHONDONTWRITEBYTECODE": "1", "HOME": str(tmp), "USERPROFILE": str(tmp)}
        for name in ("PHOTO_SORTER_DB", "PHOTO_SORTER_UI_ARGS", "PHOTO_SORTER_FACES_ARGS", "DEPLOY_TARGET", "KEEP_BACKUPS", "DEPLOY_CONF"):
            self.env.pop(name, None)

    def run(self, script, *args, stdin="", **env):
        return subprocess.run([sys.executable, str(self.app / "scripts" / script), *args], input=stdin, capture_output=True, text=True,
                              env={**self.env, **{k: str(v) for k, v in env.items()}}, cwd=self.tmp, timeout=180)

    def calls(self):
        return self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []

    def commands(self):
        return [c.split()[0] for c in self.calls()]


@pytest.fixture
def box(tmp_path):
    return Box(tmp_path)


@pytest.fixture
def folders(tmp_path):
    src = tmp_path / "incoming" / "my photos"
    src.mkdir(parents=True)
    lib = tmp_path / "library"
    lib.mkdir()
    return src, lib


def answers(*lines):
    return "".join(f"{line}\n" for line in lines)


# ===================================================================== launchers
@pytest.mark.parametrize("bat, script", [("photo-sorter.bat", "menu.py"), ("run-tests.bat", "run_tests.py"), ("deploy.bat", "deploy.py")])
def test_launchers_use_windows_line_endings_and_start_the_right_script(bat, script):
    data = (ROOT / bat).read_bytes()
    assert data.count(b"\r\n") == data.count(b"\n") > 3, "a .bat file needs CRLF line endings"
    text = data.decode("ascii")
    assert f"scripts\\{script}" in text and "find-python.bat" in text and "%*" in text and "exit /b" in text
    assert (ROOT / "scripts" / script).is_file()


def test_find_python_batch_file_is_consistent():
    data = (ROOT / "scripts" / "find-python.bat").read_bytes()
    assert data.count(b"\r\n") == data.count(b"\n") > 5
    text = data.decode("ascii")
    for needle in ("PHOTO_SORTER_PYTHON", ".venv\\Scripts\\python.exe", "py -3", "pycheck.py"):
        assert needle in text
    assert "setlocal" not in text.lower()                     # PY must reach the caller
    assert subprocess.run([sys.executable, str(ROOT / "scripts" / "pycheck.py")]).returncode == 0


def test_every_script_file_parses():
    import ast
    for name in ("menu.py", "run_tests.py", "deploy.py", "_cli.py", "pycheck.py"):
        ast.parse((ROOT / "scripts" / name).read_text(encoding="utf-8"))


# ===================================================================== menu.py
def test_menu_quit_eof_and_bad_choices(box):
    r = box.run("menu.py", stdin="q\n")
    assert r.returncode == 0 and "1) Start the web interface" in r.stdout and "3) Find faces" in r.stdout and "4) Read camera details" in r.stdout and "5) Prepare search" in r.stdout and "6) Check a search model" in r.stdout and "7) Reset the web interface password" in r.stdout
    assert box.run("menu.py", stdin="").returncode == 0
    r = box.run("menu.py", stdin="9\nhello\n\nQ\n")
    assert r.stderr.count("Please type 1, 2, 3, 4, 5, 6, 7 or q.") == 2 and box.calls() == []


def test_menu_help_and_unknown_argument(box):
    assert box.run("menu.py", "--help").returncode == 0
    r = box.run("menu.py", "bogus")
    assert r.returncode == 2 and "Unknown option: bogus" in r.stderr


def test_menu_choice_runs_the_action_then_returns(box):
    r = box.run("menu.py", stdin="1\n\nq\n")
    assert r.returncode == 0 and box.calls() == ["ui"]


@pytest.mark.parametrize("word", ["ui", "1"])
def test_ui_direct_with_database_and_options(box, word, tmp_path):
    box.run("menu.py", word, PHOTO_SORTER_DB=tmp_path / "my index.db", PHOTO_SORTER_UI_ARGS="--port 9000 --no-browser")
    assert box.calls() == [f"ui --db {tmp_path / 'my index.db'} --port 9000 --no-browser"]


@pytest.mark.parametrize("word", ["password", "7"])
def test_password_option_resets_it(box, word, tmp_path):
    r = box.run("menu.py", word, PHOTO_SORTER_DB=tmp_path / "i.db")
    assert r.returncode == 0 and box.calls() == [f"password --db {tmp_path / 'i.db'}"] and "Done." in r.stdout
    box.log.unlink()
    r = box.run("menu.py", "password", FAKE_RC_password=1)
    assert r.returncode == 1 and "Nothing was changed" in r.stdout
    r = box.run("menu.py", "password", FAKE_RC_password=2)
    assert r.returncode == 2 and "could not be changed" in r.stderr
    box.log.unlink()
    r = box.run("menu.py", stdin=answers("7", "", "q"))
    assert r.returncode == 0 and box.calls() == ["password"]


def test_launcher_listens_on_the_network_unless_told_otherwise():
    text = (ROOT / "photo-sorter.bat").read_text(encoding="ascii")
    assert 'if not defined PHOTO_SORTER_UI_ARGS set "PHOTO_SORTER_UI_ARGS=--host 0.0.0.0"' in text
    assert text.lower().index("setlocal") < text.index("PHOTO_SORTER_UI_ARGS=")


def test_ui_exit_code_is_passed_on(box):
    assert box.run("menu.py", "ui", FAKE_RC_ui=2).returncode == 2


def test_sort_previews_then_applies(box, folders):
    src, lib = folders
    r = box.run("menu.py", "sort", stdin=answers(src, lib, "n", "y"))
    preview, apply = box.calls()
    assert r.returncode == 0 and preview == f"sort {src} {lib}" and apply == preview + " --apply"
    assert "Sorting finished" in r.stdout


def test_sort_move_declined_cancel_and_defaults(box, folders):
    src, lib = folders
    box.run("menu.py", "sort", stdin=answers(src, lib, "y", "y"))
    assert all(" --move" in c for c in box.calls()) and len(box.calls()) == 2
    box.log.unlink()
    box.run("menu.py", "sort", stdin=answers(src, lib, "n", "n"))
    assert box.commands() == ["sort"] and "--apply" not in box.calls()[0]
    box.log.unlink()
    assert box.run("menu.py", "sort", stdin="q\n").returncode == 0
    assert box.run("menu.py", "sort", stdin=answers(src, "q")).returncode == 0
    assert box.calls() == []


def test_sort_folder_prompts(box, folders):
    src, lib = folders
    r = box.run("menu.py", "sort", stdin=answers(box.tmp / "nowhere", "", src, box.tmp / "brand new", "n", "n"))
    assert "doesn't exist" in r.stderr and "Please enter a folder" in r.stderr and box.calls() == [f"sort {src} {box.tmp / 'brand new'}"]


def test_sort_quoted_paths_and_remembered_folders(box, folders):
    src, lib = folders
    box.run("menu.py", "sort", stdin=answers(f'"{src}"', f"'{lib}'", "n", "n"))
    assert box.calls() == [f"sort {src} {lib}"] or box.calls()[0].startswith("sort ")
    text = box.conf.read_text(encoding="utf-8")
    assert f"LAST_SOURCE={src}" in text and f"LIBRARY_DIR={lib}" in text
    box.log.unlink()
    r = box.run("menu.py", "sort", stdin=answers("", "", "n", "n"))
    assert f"[{src}]" in r.stdout and box.calls() == [f"sort {src} {lib}"]


def test_sort_failures(box, folders):
    src, lib = folders
    r = box.run("menu.py", "sort", stdin=answers(src, lib, "n", "y"), FAKE_RC_sort=2)
    assert r.returncode == 1 and box.commands() == ["sort"]
    box.log.unlink()
    r = box.run("menu.py", "sort", stdin=answers(src, lib, "n", "y"), FAKE_RC_sort=1)
    assert "reported some errors" in r.stderr and box.commands() == ["sort", "sort"]
    for code in (1, 2):
        r = box.run("menu.py", "sort", stdin=answers(src, lib, "n", "y"), FAKE_RC_sort_apply=code)
        assert r.returncode == code and "problems" in r.stderr


def faces(box, lib, limit="", hw="", go="y", extra=(), **env):
    return box.run("menu.py", "faces", stdin=answers(lib, limit, hw, go, *extra), **env)


def test_faces_runs_three_steps_in_order(box, folders):
    _, lib = folders
    r = faces(box, lib)
    assert r.returncode == 0 and box.calls() == [f"refresh {lib} --apply", "faces --apply", "cluster --apply"]


def test_faces_limit_hardware_and_extra_options(box, folders, tmp_path):
    _, lib = folders
    faces(box, lib, limit="50", hw="g")
    assert box.calls()[1] == "faces --apply --limit 50 --gpu"
    box.log.unlink()
    faces(box, lib, limit="7", hw="CPU")
    assert box.calls()[1] == "faces --apply --limit 7 --cpu"
    box.log.unlink()
    faces(box, lib, PHOTO_SORTER_DB=tmp_path / "d.db", PHOTO_SORTER_FACES_ARGS="--det-size 960")
    assert box.calls()[1] == f"faces --apply --db {tmp_path / 'd.db'} --det-size 960"


@pytest.mark.parametrize("bad", ["abc", "0", "-5", "1.5"])
def test_faces_bad_limit_is_ignored(box, folders, bad):
    _, lib = folders
    r = faces(box, lib, limit=bad)
    assert "not a number" in r.stderr and box.calls()[1] == "faces --apply"


def test_faces_cancel_decline_and_saved_library(box, folders):
    _, lib = folders
    assert box.run("menu.py", "faces", stdin="q\n").returncode == 0
    assert faces(box, lib, go="n").returncode == 0 and box.calls() == []
    faces(box, lib)
    box.log.unlink()
    r = box.run("menu.py", "faces", stdin=answers("", "", "", "y"))
    assert f"[{lib}]" in r.stdout and box.calls()[0] == f"refresh {lib} --apply"


def test_faces_stop_and_continue_rules(box, folders):
    _, lib = folders
    r = faces(box, lib, FAKE_RC_refresh_apply=2)
    assert r.returncode == 1 and box.commands() == ["refresh"]
    box.log.unlink()
    assert faces(box, lib, extra=["n"], FAKE_RC_refresh_apply=1).returncode == 1 and box.commands() == ["refresh"]
    box.log.unlink()
    assert faces(box, lib, extra=["y"], FAKE_RC_refresh_apply=1).returncode == 0 and box.commands() == ["refresh", "faces", "cluster"]
    box.log.unlink()
    r = faces(box, lib, FAKE_RC_faces_apply=130)
    assert r.returncode == 130 and box.commands() == ["refresh", "faces"]
    box.log.unlink()
    assert faces(box, lib, FAKE_RC_faces_apply=2).returncode == 1 and box.commands() == ["refresh", "faces"]
    box.log.unlink()
    r = faces(box, lib, FAKE_RC_faces_apply=1)
    assert r.returncode == 0 and box.commands() == ["refresh", "faces", "cluster"]
    r = faces(box, lib, FAKE_RC_cluster_apply=1)
    assert r.returncode == 1 and "Grouping did not finish" in r.stderr


# ----------------------------------------------------------------- 4) camera details
@pytest.mark.parametrize("word", ["metadata", "4"])
def test_metadata_previews_then_reads(box, word, tmp_path):
    r = box.run("menu.py", word, stdin=answers("", "y"), PHOTO_SORTER_DB=tmp_path / "i.db")
    db = tmp_path / "i.db"
    assert r.returncode == 0 and box.calls() == [f"metadata --db {db}", f"metadata --apply --db {db}"]
    assert "Done." in r.stdout


def test_metadata_reread_decline_and_menu_return(box):
    box.run("menu.py", "metadata", stdin=answers("y", ""))
    assert box.calls() == ["metadata --reread", "metadata --apply --reread"]
    box.log.unlink()
    r = box.run("menu.py", "metadata", stdin=answers("n", "n"))
    assert r.returncode == 0 and box.calls() == ["metadata"] and "Nothing was changed" in r.stdout
    box.log.unlink()
    r = box.run("menu.py", stdin=answers("4", "", "", "", "q"))
    assert r.returncode == 0 and box.commands() == ["metadata", "metadata"]


def test_metadata_stop_rules(box):
    r = box.run("menu.py", "metadata", stdin=answers("", "y"), FAKE_RC_metadata=2)
    assert r.returncode == 1 and "no index yet" in r.stderr and box.calls() == ["metadata"]
    box.log.unlink()
    assert box.run("menu.py", "metadata", stdin=answers("", "y"), FAKE_RC_metadata=1).returncode == 1 and box.calls() == ["metadata"]
    for rc, text in ((1, "could not be read"), (130, "Run this option again"), (3, "did not finish")):
        r = box.run("menu.py", "metadata", stdin=answers("", "y"), FAKE_RC_metadata_apply=rc)
        assert r.returncode == rc and text in r.stderr, rc


# ----------------------------------------------------------------- 5) prepare search
DFN = "ViT-L-14-quickgelu__dfn2b"
SIGLIP = "ViT-B-16-SigLIP2__webli"


@pytest.mark.parametrize("word", ["search", "5"])
def test_search_previews_then_processes(box, word, tmp_path):
    db = tmp_path / "i.db"
    r = box.run("menu.py", word, stdin=answers("", "y"), PHOTO_SORTER_DB=db)
    assert r.returncode == 0 and box.calls() == [f"embed --model {DFN} --db {db}", f"embed --apply --model {DFN} --db {db}"]
    assert "Done." in r.stdout


def test_search_model_choice_is_remembered_decline_and_menu_return(box):
    box.run("menu.py", "search", stdin=answers(f' "{SIGLIP}" ', "y"))
    assert box.calls() == [f"embed --model {SIGLIP}", f"embed --apply --model {SIGLIP}"]
    box.log.unlink()
    r = box.run("menu.py", "search", stdin=answers("", "n"))
    assert r.returncode == 0 and box.calls() == [f"embed --model {SIGLIP}"] and "Nothing was changed" in r.stdout
    assert SIGLIP in r.stdout  # offered as the default
    box.log.unlink()
    r = box.run("menu.py", stdin=answers("5", "", "", "", "q"))
    assert r.returncode == 0 and box.commands() == ["embed", "embed"]


def test_search_offers_to_download_a_missing_model(box):
    r = box.run("menu.py", "search", stdin=answers("", "y", "y"), FAKE_RC_embed=3)
    assert r.returncode == 0 and box.calls() == [
        f"embed --model {DFN}", f"clipcheck --model {DFN} --download --no-cpu-compare", f"embed --apply --model {DFN}"]
    box.log.unlink()
    r = box.run("menu.py", "search", stdin=answers("", "n"), FAKE_RC_embed=3)
    assert r.returncode == 0 and box.commands() == ["embed"] and "Nothing was changed" in r.stdout
    box.log.unlink()
    r = box.run("menu.py", "search", stdin=answers("", "y", "y"), FAKE_RC_embed=3, FAKE_RC_clipcheck_download=1)
    assert r.returncode == 1 and box.commands() == ["embed", "clipcheck"] and "could not be downloaded" in r.stderr
    box.log.unlink()
    r = box.run("menu.py", "search", stdin=answers("", "y", "y"), FAKE_RC_embed=3, FAKE_RC_clipcheck_download=130)
    assert r.returncode == 130 and box.commands() == ["embed", "clipcheck"] and "fetch the rest" in r.stderr


def test_search_stop_rules(box):
    r = box.run("menu.py", "search", stdin=answers("", "y"), FAKE_RC_embed=2)
    assert r.returncode == 1 and "is there an index yet" in r.stderr and box.calls() == [f"embed --model {DFN}"]
    box.log.unlink()
    assert box.run("menu.py", "search", stdin=answers("", "y"), FAKE_RC_embed=1).returncode == 1 and box.commands() == ["embed"]
    for rc, text in ((1, "tried again next time"), (130, "Run this option again"), (4, "did not finish")):
        r = box.run("menu.py", "search", stdin=answers("", "y"), FAKE_RC_embed_apply=rc)
        assert r.returncode == rc and text in r.stderr, rc


# ----------------------------------------------------------------- 6) check a search model
@pytest.mark.parametrize("word", ["clipcheck", "6"])
def test_clipcheck_lists_checks_and_remembers_the_model(box, word, tmp_path):
    db = tmp_path / "i.db"
    r = box.run("menu.py", word, stdin=answers(""), PHOTO_SORTER_DB=db)
    assert r.returncode == 0 and box.calls() == ["clipcheck --list", f"clipcheck --model {DFN} --db {db}"]
    assert "The model works" in r.stdout and f"SEARCH_MODEL={DFN}" in box.conf.read_text(encoding="utf-8")


def test_clipcheck_choice_becomes_the_default_for_option_5(box):
    assert box.run("menu.py", "clipcheck", stdin=answers(SIGLIP)).returncode == 0
    box.log.unlink()
    box.run("menu.py", "search", stdin=answers("", "n"))
    assert box.calls() == [f"embed --model {SIGLIP}"]
    box.log.unlink()
    r = box.run("menu.py", stdin=answers("6", "", "", "q"))
    assert r.returncode == 0 and box.calls() == ["clipcheck --list", f"clipcheck --model {SIGLIP}"]


def test_clipcheck_downloads_only_after_asking(box):
    r = box.run("menu.py", "clipcheck", stdin=answers("", "y"), FAKE_RC_clipcheck=3)
    assert r.returncode == 0 and box.calls() == ["clipcheck --list", f"clipcheck --model {DFN}", f"clipcheck --model {DFN} --download"]
    box.log.unlink()
    r = box.run("menu.py", "clipcheck", stdin=answers("", "n"), FAKE_RC_clipcheck=3)
    assert r.returncode == 0 and "Nothing was downloaded" in r.stdout and not any("--download" in c for c in box.calls())
    for rc, text in ((1, "did not pass the check"), (130, "continues where it stopped"), (2, "could not run")):
        r = box.run("menu.py", "clipcheck", stdin=answers("", "y"), FAKE_RC_clipcheck=3, FAKE_RC_clipcheck_download=rc)
        assert r.returncode == rc and text in r.stderr, rc


def test_clipcheck_failures_are_not_remembered(box):
    for rc, text in ((1, "did not pass the check"), (2, "is the model name right")):
        r = box.run("menu.py", "clipcheck", stdin=answers("bad"), FAKE_RC_clipcheck=rc)
        assert r.returncode == rc and text in r.stderr, rc
        assert not box.conf.exists() or "SEARCH_MODEL" not in box.conf.read_text(encoding="utf-8")


def test_missing_packages_are_offered_for_installation(box, folders, monkeypatch):
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import importlib
        menu = importlib.import_module("menu")
        monkeypatch.setattr(menu.importlib.util, "find_spec", lambda name: None)
        monkeypatch.setattr("builtins.input", lambda prompt="": "n")
        assert menu.ensure_modules("X", "requirements-ui.txt", "fastapi") is False
        monkeypatch.setattr("builtins.input", lambda prompt="": "y")
        installed = []
        monkeypatch.setattr(menu.subprocess, "call", lambda cmd, **kw: installed.append(cmd) or 0)
        assert menu.ensure_modules("X", "requirements-ui.txt", "fastapi") is True and installed[0][-2:] == ["-r", "requirements-ui.txt"]
        monkeypatch.setattr(menu.subprocess, "call", lambda cmd, **kw: 1)
        assert menu.ensure_modules("X", "requirements-ui.txt", "fastapi") is False
    finally:
        sys.path.remove(str(ROOT / "scripts"))
        sys.modules.pop("menu", None)
        sys.modules.pop("_cli", None)


@pytest.mark.parametrize("typed, expected", [("  C:\\a\\b  ", "C:\\a\\b"), ('"C:\\my photos"', "C:\\my photos"), ("'/a/b c'", "/a/b c"), ("plain", "plain"), ("", "")])
def test_clean_path(typed, expected):
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import _cli
        assert _cli.clean_path(typed) == expected
    finally:
        sys.path.remove(str(ROOT / "scripts"))
        sys.modules.pop("_cli", None)


# ===================================================================== run_tests.py
def test_run_tests_default_skips_browser_tests_and_passes(box):
    r = box.run("run_tests.py")
    assert r.returncode == 0 and "All tests passed" in r.stdout and "1 passed" in r.stdout and "1 deselected" in r.stdout


def test_run_tests_modes_and_options(box):
    assert box.run("run_tests.py", "--e2e").returncode == 1
    assert box.run("run_tests.py", "--all").returncode == 1
    r = box.run("run_tests.py", "-k", "plain", "-v")
    assert r.returncode == 0 and "test_plain" in r.stdout
    r = box.run("run_tests.py", "--", "-k", "nothing_matches_this")
    assert r.returncode == 5


def test_run_tests_unknown_option_and_help(box):
    assert box.run("run_tests.py", "--nonsense").returncode == 2
    assert box.run("run_tests.py", "--help").returncode == 0


def test_run_tests_static_checks(box):
    (box.app / "broken.py").write_text("def oops(:\n", encoding="utf-8")
    r = box.run("run_tests.py")
    assert r.returncode == 1 and "broken.py" in r.stdout and "Fix the syntax errors" in r.stderr
    r = box.run("run_tests.py", "--no-static")
    assert r.returncode == 0


# ===================================================================== deploy.py
def deploy(box, target, *args, stdin="", **env):
    return box.run("deploy.py", "--target", str(target), "--skip-tests", "--yes", *args, stdin=stdin, **env)


def listing(root: Path):
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())


def test_deploy_refuses_placeholders(box, tmp_path):
    for args in ([], ["--target", PLACEHOLDER], ["--target", ""]):
        r = box.run("deploy.py", *args, "--skip-tests", "--yes")
        assert r.returncode == 2 and "placeholder" in r.stderr
    assert box.run("deploy.py", "--yes", "--skip-tests", DEPLOY_TARGET=PLACEHOLDER).returncode == 2
    conf = tmp_path / "d.conf"
    conf.write_text(f"DEPLOY_TARGET={PLACEHOLDER}\n", encoding="utf-8")
    assert box.run("deploy.py", "--yes", "--skip-tests", DEPLOY_CONF=conf).returncode == 2
    assert box.run("deploy.py", "--target", PLACEHOLDER, "--rollback", "--yes").returncode == 2


def test_deploy_target_from_environment_and_config(box, tmp_path):
    assert box.run("deploy.py", "--yes", "--skip-tests", DEPLOY_TARGET=tmp_path / "env").returncode == 0 and (tmp_path / "env" / "sort_photos.py").exists()
    conf = tmp_path / "my.conf"
    conf.write_text(f"# c\nDEPLOY_TARGET={tmp_path / 'conf'}\nKEEP_BACKUPS=3\n", encoding="utf-8")
    assert box.run("deploy.py", "--yes", "--skip-tests", DEPLOY_CONF=conf).returncode == 0 and (tmp_path / "conf" / "sort_photos.py").exists()


def test_deploy_dry_run(box, tmp_path):
    target = tmp_path / "prod"
    r = box.run("deploy.py", "--target", target, "--dry-run")
    assert r.returncode == 0 and not target.exists() and "sort_photos.py" in r.stdout and "Dry run" in r.stdout


def test_deploy_copies_only_program_files(box, tmp_path):
    (box.app / "stray.db").write_bytes(b"x")
    (box.app / "deploy.conf").write_text("DEPLOY_TARGET=/secret\n", encoding="utf-8")
    target = tmp_path / "prod"
    r = deploy(box, target)
    assert r.returncode == 0 and "every file matches" in r.stdout and "sort_photos.py starts" in r.stdout
    files = listing(target)
    for wanted in ("sort_photos.py", "ui_server.py", "web/app.js", "README.md", "photo-sorter.bat", "photo-sorter.sh", "scripts/menu.py", "scripts/_cli.py",
                   "scripts/find-python.bat", "requirements-ui.txt", "VERSION", ".deployed-files"):
        assert wanted in files, wanted
    for unwanted in ("run-tests.bat", "deploy.bat", "run-tests.sh", "deploy.sh", "scripts/deploy.py", "scripts/run_tests.py", "requirements-dev.txt", "stray.db", "deploy.conf"):
        assert unwanted not in files, unwanted
    assert not any(f.startswith("tests/") or f.endswith(".pyc") for f in files)
    assert set((target / ".deployed-files").read_text(encoding="utf-8").split()) == set(files)


def test_deploy_never_touches_data_config_or_venv(box, tmp_path):
    target = tmp_path / "prod"
    (target / "thumbs").mkdir(parents=True)
    (target / ".venv" / "Scripts").mkdir(parents=True)
    keep = {"photo_index.db": b"d", ".photo-sorter.conf": b"x=1\n", "thumbs/a.jpg": b"t", ".venv/Scripts/python.exe": b"p", "notes.txt": b"mine"}
    for rel, data in keep.items():
        (target / rel).write_bytes(data)
    assert deploy(box, target).returncode == 0 and deploy(box, target).returncode == 0
    for rel, data in keep.items():
        assert (target / rel).read_bytes() == data


def test_deploy_confirmation(box, tmp_path):
    target = tmp_path / "prod"
    r = box.run("deploy.py", "--target", target, "--skip-tests", stdin="n\n")
    assert r.returncode == 0 and not target.exists() and "Nothing was changed" in r.stdout
    assert box.run("deploy.py", "--target", target, "--skip-tests", stdin="y\n").returncode == 0 and (target / "sort_photos.py").exists()


def test_backups_pruning_and_no_backup(box, tmp_path):
    target = tmp_path / "prod"
    r = deploy(box, target)
    assert "first deployment" in r.stdout and not Path(f"{target}-backups").exists()
    original = (target / "README.md").read_text(encoding="utf-8")
    (box.app / "README.md").write_text("v2\n", encoding="utf-8")
    deploy(box, target)
    backups = Path(f"{target}-backups")
    stamps = sorted(backups.iterdir())
    assert len(stamps) == 1 and (stamps[0] / "README.md").read_text(encoding="utf-8") == original
    for name in ("20200101-000000", "20210101-000000", "20220101-000000", "20230101-000000"):
        (backups / name).mkdir()
    (box.app / "README.md").write_text("v3\n", encoding="utf-8")
    deploy(box, target, "--keep", "2")
    left = sorted(p.name for p in backups.iterdir())
    # Two real deploys made backups (v2 and v3); they are newer than the fake ones, so only they stay.
    # (If both deploys ran in the same second they share one folder, and the newest fake one survives.)
    assert len(left) == 2 and left[1] > "2024" and (left[0] > "2024" or left[0] == "20230101-000000")
    (box.app / "README.md").write_text("v4\n", encoding="utf-8")
    shutil.rmtree(backups)
    r = deploy(box, target, "--no-backup")
    assert "No backup" in r.stderr and not backups.exists()


def test_deploy_removes_stale_program_files_only(box, tmp_path):
    target = tmp_path / "prod"
    (box.app / "old_module.py").write_text("x = 1\n", encoding="utf-8")
    deploy(box, target)
    (target / "mine.txt").write_text("user", encoding="utf-8")
    (box.app / "old_module.py").unlink()
    r = deploy(box, target)
    assert not (target / "old_module.py").exists() and (target / "mine.txt").exists() and "1 old file(s) removed" in r.stdout


def test_deploy_option_errors(box, tmp_path):
    assert box.run("deploy.py", "--bogus").returncode == 2
    r = box.run("deploy.py", "--target", tmp_path / "p", "--keep", "many", "--skip-tests", "--yes")
    assert r.returncode == 1 and not (tmp_path / "p").exists()


def test_deploy_runs_tests_first(box, tmp_path):
    target = tmp_path / "prod"
    (box.app / "tests" / "test_x.py").write_text("def test_bad(): assert False\n", encoding="utf-8")
    r = box.run("deploy.py", "--target", target, "--yes")
    assert r.returncode == 1 and "nothing was deployed" in r.stderr and not target.exists()
    (box.app / "tests" / "test_x.py").write_text("def test_good(): pass\n", encoding="utf-8")
    r = box.run("deploy.py", "--target", target, "--yes")
    assert r.returncode == 0 and (target / "sort_photos.py").exists()


def test_deploy_target_with_spaces_and_quotes(box, tmp_path):
    target = tmp_path / "my production folder"
    assert box.run("deploy.py", "--target", f'"{target}"', "--skip-tests", "--yes").returncode == 0 and (target / "sort_photos.py").exists()


def test_deploy_reports_a_start_up_problem(box, tmp_path):
    (box.app / "sort_photos.py").write_text("import sys\nsys.exit(1)\n", encoding="utf-8")
    r = deploy(box, tmp_path / "prod")
    assert r.returncode == 1 and "did not start" in r.stderr


def test_rollback(box, tmp_path):
    target = tmp_path / "prod"
    assert box.run("deploy.py", "--target", target, "--rollback", "--yes").returncode == 1
    deploy(box, target)
    v1 = (target / "README.md").read_text(encoding="utf-8")
    (box.app / "README.md").write_text("v2\n", encoding="utf-8")
    (box.app / "new_in_v2.py").write_text("x = 2\n", encoding="utf-8")
    deploy(box, target)
    (target / "photo_index.db").write_bytes(b"data")
    r = box.run("deploy.py", "--target", target, "--rollback", stdin="n\n")
    assert (target / "README.md").read_text(encoding="utf-8") == "v2\n"
    r = box.run("deploy.py", "--target", target, "--rollback", "--dry-run", "--yes")
    assert "Dry run" in r.stdout and (target / "new_in_v2.py").exists()
    r = box.run("deploy.py", "--target", target, "--rollback", "--yes")
    assert r.returncode == 0 and "Rolled back" in r.stdout
    assert (target / "README.md").read_text(encoding="utf-8") == v1 and not (target / "new_in_v2.py").exists()
    assert (target / "photo_index.db").read_bytes() == b"data" and any(Path(f"{target}-backups").iterdir())


# ===================================================================== self-update.bat (Windows with git only)
needs_cmd_and_git = pytest.mark.skipif(os.name != "nt" or shutil.which("git") is None, reason="runs the real .bat files with cmd and git")

FAKE_MENU = 'import sys\nprint("menu VERSION", " ".join(sys.argv[1:]))\n'


class Upstream:
    """A local stand-in for GitHub (a bare repo) plus a clone of it, holding the real launchers and a fake menu."""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.origin = tmp / "origin.git"
        self.work = tmp / "upstream-work"
        self.clone = tmp / "my copy"
        self.git(None, "init", "--bare", "-b", "main", str(self.origin))
        self.git(None, "clone", str(self.origin), str(self.work))
        (self.work / "scripts").mkdir()
        for rel in ("photo-sorter.bat", "scripts/self-update.bat", "scripts/find-python.bat", "scripts/pycheck.py"):
            shutil.copyfile(ROOT / rel, self.work / rel)
        (self.work / ".gitattributes").write_text("*.bat -text\n", encoding="utf-8")
        self.publish("first", {"scripts/menu.py": FAKE_MENU.replace("VERSION", "v1"), "requirements-ui.txt": "fastapi\n"})
        self.git(None, "clone", str(self.origin), str(self.clone))

    def git(self, where, *args):
        cmd = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "core.autocrlf=false", "-c", "init.defaultBranch=main"]
        cmd += (["-C", str(where)] if where else []) + list(args)
        return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout

    def publish(self, message, files):
        for rel, text in files.items():
            (self.work / rel).write_bytes(text.encode("utf-8"))
        self.git(self.work, "add", "-A")
        self.git(self.work, "commit", "-q", "-m", message)
        self.git(self.work, "push", "-q", "origin", "main")

    def run(self, bat, *args, **env):
        environ = {**os.environ, "PHOTO_SORTER_PYTHON": f'"{sys.executable}"', **env}
        for name in ("PHOTO_SORTER_NO_UPDATE", "PHOTO_SORTER_UPDATED"):
            if name not in env:
                environ.pop(name, None)
        return subprocess.run(["cmd", "/d", "/c", str(self.clone / bat), *args], capture_output=True, text=True, env=environ, timeout=120)

    def head(self):
        return self.git(self.clone, "rev-parse", "HEAD").strip()


@pytest.fixture
def upstream(tmp_path):
    return Upstream(tmp_path)


@needs_cmd_and_git
def test_launcher_updates_then_starts_the_new_version(upstream):
    r = upstream.run("photo-sorter.bat", "q")
    assert r.returncode == 0 and "Already up to date" in r.stdout and "menu v1 q" in r.stdout
    longer = (ROOT / "photo-sorter.bat").read_text(encoding="ascii").replace("@echo off\n", "@echo off\nrem " + "x" * 300 + "\n", 1)
    upstream.publish("second", {"scripts/menu.py": FAKE_MENU.replace("VERSION", "v2"), "requirements-ui.txt": "fastapi\nnumpy\n",
                                "photo-sorter.bat": longer.replace("\r\n", "\n").replace("\n", "\r\n")})
    r = upstream.run("photo-sorter.bat", "q")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Updated to the latest version" in r.stdout and "second" in r.stdout
    assert "requirements-ui.txt changed" in r.stdout
    assert r.stdout.count("menu v2 q") == 1 and "menu v1" not in r.stdout       # restarted once, from the new copy
    assert upstream.run("photo-sorter.bat", "q").stdout.count("Already up to date") == 1


@needs_cmd_and_git
def test_menu_exit_code_still_comes_through(upstream):
    upstream.publish("fails", {"scripts/menu.py": "import sys\nsys.exit(4)\n"})
    assert upstream.run("photo-sorter.bat").returncode == 4
    assert upstream.run("photo-sorter.bat").returncode == 4


@needs_cmd_and_git
def test_update_can_be_turned_off(upstream):
    upstream.publish("second", {"scripts/menu.py": FAKE_MENU.replace("VERSION", "v2")})
    r = upstream.run("photo-sorter.bat", PHOTO_SORTER_NO_UPDATE="1")
    assert "Checking GitHub" not in r.stdout and "menu v1" in r.stdout


@needs_cmd_and_git
def test_local_changes_and_local_commits_are_never_touched(upstream):
    upstream.publish("second", {"scripts/menu.py": FAKE_MENU.replace("VERSION", "v2")})
    before = upstream.head()
    (upstream.clone / "requirements-ui.txt").write_text("my own edit\n", encoding="utf-8")
    r = upstream.run("scripts/self-update.bat")
    assert r.returncode == 0 and "files in this copy were changed" in r.stdout and upstream.head() == before
    assert (upstream.clone / "requirements-ui.txt").read_text(encoding="utf-8") == "my own edit\n"

    upstream.git(upstream.clone, "commit", "-q", "-am", "mine")
    mine = upstream.head()
    r = upstream.run("scripts/self-update.bat")
    assert r.returncode == 0 and "its own commits" in r.stdout and upstream.head() == mine


@needs_cmd_and_git
def test_other_branch_no_network_and_no_git_checkout_are_skipped(upstream):
    upstream.git(upstream.clone, "checkout", "-q", "-b", "trying-something")
    r = upstream.run("scripts/self-update.bat")
    assert r.returncode == 0 and 'branch "trying-something"' in r.stdout
    upstream.git(upstream.clone, "checkout", "-q", "main")

    upstream.git(upstream.clone, "remote", "set-url", "origin", str(upstream.tmp / "gone.git"))
    r = upstream.run("photo-sorter.bat", "q")
    assert r.returncode == 0 and "Could not reach GitHub" in r.stdout and "menu v1 q" in r.stdout

    shutil.rmtree(upstream.clone / ".git", onerror=lambda f, p, e: (os.chmod(p, 0o700), f(p)))
    r = upstream.run("photo-sorter.bat", "q")
    assert r.returncode == 0 and "Checking GitHub" not in r.stdout and "menu v1 q" in r.stdout


def test_self_update_batch_file_is_consistent():
    data = (ROOT / "scripts" / "self-update.bat").read_bytes()
    assert data.count(b"\r\n") == data.count(b"\n") > 10
    text = data.decode("ascii")
    assert "--ff-only" in text and "PHOTO_SORTER_NO_UPDATE" in text and "exit /b 3" in text
    launcher = (ROOT / "photo-sorter.bat").read_text(encoding="ascii")
    assert "self-update.bat" in launcher and "PHOTO_SORTER_UPDATED" in launcher
