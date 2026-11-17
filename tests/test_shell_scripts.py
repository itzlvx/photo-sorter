"""The three bash scripts: photo-sorter.sh (menu), run-tests.sh and deploy.sh.

They are run for real with bash, but with a *fake Python* that only records the commands it is given, so nothing
is sorted, scanned or installed. Skipped when bash is not available.
"""
import os
import shutil
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BASH = shutil.which("bash")
if sys.platform == "win32":
    BASH = None      # the bash on PATH is usually WSL, which cannot use C:\ paths; these tests run on Linux / macOS / CI
pytestmark = [pytest.mark.shell, pytest.mark.skipif(BASH is None, reason="bash is not installed")]

FAKE_PYTHON = textwrap.dedent('''\
    #!/usr/bin/env bash
    # Stands in for python: answers the version / import probes and records every real command.
    case "$1" in
      -c)
        case "$2" in *version_info*) exit "${FAKE_OLD_PYTHON:-0}" ;; esac
        for m in ${FAKE_MISSING:-}; do [ "$2" = "import $m" ] && exit 1; done
        exit 0 ;;
      -)  cat >/dev/null; exit "${FAKE_STATIC_RC:-0}" ;;
      -m)
        echo "$*" >>"$FAKE_LOG"
        case "$2" in
          pytest) exit "${FAKE_PYTEST_RC:-0}" ;;
          pip)    exit "${FAKE_PIP_RC:-0}" ;;
          *)      exit 0 ;;
        esac ;;
      sort_photos.py)
        echo "$*" >>"$FAKE_LOG"
        name="${2//[^a-z]/_}"
        case " $* " in *" --apply "*) var="FAKE_RC_${name}_apply" ;; *" --download "*) var="FAKE_RC_${name}_download" ;; *) var="FAKE_RC_${name}" ;; esac
        exit "${!var:-0}" ;;
    esac
    exit 0
''')


class Sandbox:
    """A private copy of the program folder, a fake python and a place for the remembered answers."""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.app = tmp / "app"
        shutil.copytree(ROOT, self.app, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", ".git", ".venv", "venv", "*.db", ".coverage", "deploy.conf", ".photo-sorter.conf", "tests"))
        self.fake = tmp / "fakepython"
        self.fake.write_text(FAKE_PYTHON, encoding="utf-8", newline="\n")
        self.fake.chmod(self.fake.stat().st_mode | stat.S_IEXEC)
        self.log = tmp / "calls.log"
        self.conf = tmp / "answers.conf"
        self.env = {**os.environ, "PHOTO_SORTER_PYTHON": str(self.fake), "FAKE_LOG": str(self.log), "PHOTO_SORTER_CONF": str(self.conf),
                    "NO_COLOR": "1", "HOME": str(tmp / "home")}
        for name in ("PHOTO_SORTER_DB", "PHOTO_SORTER_UI_ARGS", "PHOTO_SORTER_FACES_ARGS", "DEPLOY_TARGET", "KEEP_BACKUPS", "DEPLOY_CONF"):
            self.env.pop(name, None)
        (tmp / "home").mkdir()

    def run(self, script, *args, stdin="", **env):
        done = subprocess.run([BASH, str(self.app / script), *args], input=stdin, capture_output=True, text=True,
                              env={**self.env, **{k: str(v) for k, v in env.items()}}, cwd=self.tmp, timeout=120)
        done.calls = self.calls()
        return done

    def calls(self):
        return self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []

    def commands(self):
        """The sort_photos.py sub-commands that were run, in order."""
        return [line.split()[1] for line in self.calls() if line.startswith("sort_photos.py")]

    def conf_text(self):
        return self.conf.read_text(encoding="utf-8") if self.conf.exists() else ""


@pytest.fixture
def box(tmp_path):
    return Sandbox(tmp_path)


@pytest.fixture
def folders(tmp_path):
    src = tmp_path / "incoming" / "my photos"
    src.mkdir(parents=True)
    lib = tmp_path / "library"
    lib.mkdir()
    return src, lib


# ===================================================================== all scripts parse
@pytest.mark.parametrize("script", ["photo-sorter.sh", "run-tests.sh", "deploy.sh", "scripts/common.sh"])
def test_scripts_are_valid_bash_with_unix_line_endings(script):
    assert subprocess.run([BASH, "-n", str(ROOT / script)]).returncode == 0
    assert b"\r" not in (ROOT / script).read_bytes()


@pytest.mark.parametrize("script", ["photo-sorter.sh", "run-tests.sh", "deploy.sh"])
def test_scripts_are_executable_and_have_a_shebang(script):
    path = ROOT / script
    assert os.access(path, os.X_OK) or sys.platform == "win32"
    assert path.read_text(encoding="utf-8").startswith("#!/usr/bin/env bash")


@pytest.mark.parametrize("script", ["photo-sorter.sh", "run-tests.sh", "deploy.sh"])
def test_help_works_without_python(box, script):
    r = box.run(script, "--help", PHOTO_SORTER_PYTHON="/nonexistent/python")
    assert r.returncode == 0 and r.stdout.strip() and not box.calls()


# ===================================================================== common.sh helpers
def helper(box, snippet, **env):
    code = f'APP_DIR="{box.app}"; . "{box.app}/scripts/common.sh"; {snippet}'
    return subprocess.run([BASH, "-c", code], capture_output=True, text=True, env={**box.env, **env}, cwd=box.tmp)


@pytest.mark.parametrize("typed, expected", [
    ("  /a/b  ", "/a/b"), ('"/a/my photos"', "/a/my photos"), ("'/a/b c'", "/a/b c"), ('  "C:\\Users\\Jeff\\Pictures"  ', "C:\\Users\\Jeff\\Pictures"),
    ("~/pics", None), ("plain", "plain"), ("", ""),
])
def test_clean_path(box, typed, expected):
    out = subprocess.run([BASH, "-c", f'APP_DIR="{box.app}"; . "{box.app}/scripts/common.sh"; clean_path "$1"', "x", typed],
                         capture_output=True, text=True, env=box.env).stdout
    assert out == (expected if expected is not None else f"{box.env['HOME']}/pics")


def test_conf_set_and_get_round_trip_without_executing_anything(box):
    evidence = box.tmp / "evidence"
    r = helper(box, f"conf_set A 'one two'; conf_set B '$(touch {evidence})'; conf_set A three; conf_get A; echo; conf_get B; echo; conf_get MISSING; echo end")
    assert r.stdout.splitlines() == ["three", f"$(touch {evidence})", "", "end"] or r.stdout.splitlines()[:2] == ["three", f"$(touch {evidence})"]
    assert box.conf_text().count("A=") == 1 and "A=three" in box.conf_text()
    r = helper(box, "conf_get A; conf_get B")                       # reading never runs the saved text either
    assert not evidence.exists()


def test_find_python_accepts_the_override_and_rejects_a_bad_one(box):
    assert helper(box, 'find_python && echo "${PY[*]}"').stdout.strip() == str(box.fake)
    r = helper(box, 'find_python', PHOTO_SORTER_PYTHON="/nonexistent/python")
    assert r.returncode == 1 and "not Python 3.9 or newer" in r.stderr
    r = helper(box, 'find_python', FAKE_OLD_PYTHON="1")
    assert r.returncode == 1


def test_find_python_prefers_the_project_virtual_environment(box):
    venv_python = box.app / ".venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    shutil.copy(box.fake, venv_python)
    r = helper(box, 'find_python && echo "${PY[*]}"', PHOTO_SORTER_PYTHON="")
    assert r.stdout.strip() == str(venv_python)


def test_find_python_with_a_command_and_arguments(box):
    launcher = box.tmp / "py"
    shutil.copy(box.fake, launcher)
    r = helper(box, 'find_python && echo "${#PY[@]} ${PY[*]}"', PHOTO_SORTER_PYTHON=f"{launcher} -3")
    assert r.stdout.strip() == f"2 {launcher} -3"


def test_missing_python_gives_a_helpful_message_and_exit_1(box):
    r = box.run("photo-sorter.sh", "ui", PHOTO_SORTER_PYTHON="/nonexistent/python")
    assert r.returncode == 1 and "Python 3.9" in r.stderr and not r.calls


# ===================================================================== photo-sorter.sh: menu
def test_menu_quits(box):
    r = box.run("photo-sorter.sh", stdin="q\n")
    assert r.returncode == 0 and all(t in r.stdout for t in ("1) Start the web interface", "2) Intake and sort photos", "3) Find faces", "4) Read camera details", "q) Quit"))
    assert not r.calls


def test_menu_ends_cleanly_at_end_of_input(box):
    assert box.run("photo-sorter.sh", stdin="").returncode == 0


def test_menu_rejects_bad_choices_then_quits(box):
    r = box.run("photo-sorter.sh", stdin="9\nhello\n\nQ\n")
    assert r.returncode == 0 and r.stderr.count("Please type 1, 2, 3, 4, 5, 6 or q.") == 2 and not r.calls


def test_unknown_argument(box):
    r = box.run("photo-sorter.sh", "bogus")
    assert r.returncode == 2 and "Unknown option: bogus" in r.stderr


def test_menu_choice_1_starts_the_ui_then_returns_to_the_menu(box):
    r = box.run("photo-sorter.sh", stdin="1\n\nq\n")
    assert box.commands() == ["ui"] and r.returncode == 0


# ===================================================================== photo-sorter.sh: 1) web interface
@pytest.mark.parametrize("word", ["ui", "1"])
def test_ui_direct(box, word):
    r = box.run("photo-sorter.sh", word)
    assert r.returncode == 0 and box.calls() == ["sort_photos.py ui"]


def test_ui_passes_database_and_extra_options(box, tmp_path):
    box.run("photo-sorter.sh", "ui", PHOTO_SORTER_DB=tmp_path / "my index.db", PHOTO_SORTER_UI_ARGS="--port 9000 --no-browser")
    assert box.calls() == [f"sort_photos.py ui --db {tmp_path / 'my index.db'} --port 9000 --no-browser"]


def test_ui_offers_to_install_missing_packages(box):
    r = box.run("photo-sorter.sh", "ui", stdin="y\n", FAKE_MISSING="fastapi")
    assert r.returncode == 0 and "-m pip install -r requirements-ui.txt" in r.calls[0] and r.calls[-1] == "sort_photos.py ui"


def test_ui_declined_install_does_not_start(box):
    r = box.run("photo-sorter.sh", "ui", stdin="n\n", FAKE_MISSING="uvicorn")
    assert r.returncode == 1 and r.calls == [] and "uvicorn" in r.stderr


def test_ui_failed_install_does_not_start(box):
    r = box.run("photo-sorter.sh", "ui", stdin="y\n", FAKE_MISSING="fastapi", FAKE_PIP_RC=1)
    assert r.returncode == 1 and box.commands() == [] and "pip could not install" in r.stderr


def test_ui_exit_code_is_passed_on(box):
    assert box.run("photo-sorter.sh", "ui", FAKE_RC_ui=2).returncode == 2


# ===================================================================== photo-sorter.sh: 2) intake and sort
def answers(*lines):
    return "".join(f"{line}\n" for line in lines)


def test_sort_previews_then_applies(box, folders):
    src, lib = folders
    r = box.run("photo-sorter.sh", "sort", stdin=answers(src, lib, "n", "y"))
    assert r.returncode == 0 and box.commands() == ["sort", "sort"]
    preview, apply = box.calls()
    assert preview == f"sort_photos.py sort {src} {lib}" and apply == preview + " --apply"
    assert "Sorting finished" in r.stdout and "choose 3" in r.stdout.lower()


def test_sort_move_option_goes_to_both_runs(box, folders):
    src, lib = folders
    box.run("photo-sorter.sh", "sort", stdin=answers(src, lib, "y", "y"))
    preview, apply = box.calls()
    assert " --move" in preview and " --move" in apply and apply.endswith("--apply")


def test_sort_declined_after_preview_changes_nothing(box, folders):
    src, lib = folders
    r = box.run("photo-sorter.sh", "sort", stdin=answers(src, lib, "n", "n"))
    assert r.returncode == 0 and box.commands() == ["sort"] and "--apply" not in box.calls()[0] and "Nothing was changed" in r.stdout


def test_sort_default_answer_to_apply_is_no(box, folders):
    src, lib = folders
    box.run("photo-sorter.sh", "sort", stdin=answers(src, lib, "", ""))
    assert box.commands() == ["sort"]


def test_sort_cancel_at_either_prompt(box, folders):
    src, lib = folders
    assert box.run("photo-sorter.sh", "sort", stdin="q\n").returncode == 0
    assert box.run("photo-sorter.sh", "sort", stdin=answers(src, "q")).returncode == 0
    assert box.calls() == []


def test_sort_asks_again_for_a_folder_that_does_not_exist(box, folders):
    src, lib = folders
    r = box.run("photo-sorter.sh", "sort", stdin=answers(box.tmp / "nowhere", src, lib, "n", "n"))
    assert "doesn't exist" in r.stderr and box.commands() == ["sort"]


def test_sort_empty_answer_without_default_asks_again(box, folders):
    src, lib = folders
    r = box.run("photo-sorter.sh", "sort", stdin=answers("", src, lib, "n", "n"))
    assert "Please enter a folder" in r.stderr and box.commands() == ["sort"]


def test_sort_destination_does_not_have_to_exist_yet(box, folders):
    src, _ = folders
    new_lib = box.tmp / "brand new library"
    box.run("photo-sorter.sh", "sort", stdin=answers(src, new_lib, "n", "n"))
    assert box.calls() == [f"sort_photos.py sort {src} {new_lib}"]


def test_sort_accepts_quoted_paths_with_spaces(box, folders):
    src, lib = folders
    box.run("photo-sorter.sh", "sort", stdin=answers(f'"{src}"', f"'{lib}'", "n", "n"))
    assert box.calls() == [f"sort_photos.py sort {src} {lib}"] or box.calls()[0].startswith("sort_photos.py sort ")
    assert str(src).replace(" ", "\\ ") in box.calls()[0] or str(src) in box.calls()[0]


def test_sort_remembers_the_folders_and_offers_them_next_time(box, folders):
    src, lib = folders
    box.run("photo-sorter.sh", "sort", stdin=answers(src, lib, "n", "n"))
    assert f"LAST_SOURCE={src}" in box.conf_text() and f"LIBRARY_DIR={lib}" in box.conf_text()
    box.log.unlink()
    r = box.run("photo-sorter.sh", "sort", stdin=answers("", "", "n", "n"))
    assert f"[{src}]" in r.stdout and box.calls() == [f"sort_photos.py sort {src} {lib}"]


def test_sort_uses_the_database_override(box, folders, tmp_path):
    src, lib = folders
    box.run("photo-sorter.sh", "sort", stdin=answers(src, lib, "n", "y"), PHOTO_SORTER_DB=tmp_path / "x.db")
    assert all(f"--db {tmp_path / 'x.db'}" in call for call in box.calls())


def test_sort_preview_that_cannot_run_stops_before_applying(box, folders):
    src, lib = folders
    r = box.run("photo-sorter.sh", "sort", stdin=answers(src, lib, "n", "y"), FAKE_RC_sort=2)
    assert r.returncode == 1 and box.commands() == ["sort"] and "Nothing was changed" in r.stderr


def test_sort_preview_with_errors_still_lets_you_decide(box, folders):
    src, lib = folders
    r = box.run("photo-sorter.sh", "sort", stdin=answers(src, lib, "n", "y"), FAKE_RC_sort=1)
    assert "reported some errors" in r.stderr and box.commands() == ["sort", "sort"]


@pytest.mark.parametrize("code", [1, 2])
def test_sort_apply_problems_are_reported_in_the_exit_code(box, folders, code):
    src, lib = folders
    r = box.run("photo-sorter.sh", "sort", stdin=answers(src, lib, "n", "y"), FAKE_RC_sort_apply=code)
    assert r.returncode == code and "problems" in r.stderr


def test_sort_ends_cleanly_if_input_runs_out(box, folders):
    src, _ = folders
    assert box.run("photo-sorter.sh", "sort", stdin=answers(src)).returncode == 0
    assert box.calls() == []


# ===================================================================== photo-sorter.sh: 3) refresh, faces, cluster
def faces_run(box, lib, limit="", hw="", go="y", extra=(), **env):
    return box.run("photo-sorter.sh", "faces", stdin=answers(lib, limit, hw, go, *extra), **env)


def test_faces_runs_refresh_then_faces_then_cluster(box, folders):
    _, lib = folders
    r = faces_run(box, lib)
    assert r.returncode == 0 and box.commands() == ["refresh", "faces", "cluster"]
    assert box.calls() == [f"sort_photos.py refresh {lib} --apply", "sort_photos.py faces --apply", "sort_photos.py cluster --apply"]
    assert "Choose 1" in r.stdout


def test_faces_limit_and_hardware_choices(box, folders):
    _, lib = folders
    faces_run(box, lib, limit="50", hw="g")
    assert box.calls()[1] == "sort_photos.py faces --apply --limit 50 --gpu"
    box.log.unlink()
    faces_run(box, lib, limit="7", hw="CPU")
    assert box.calls()[1] == "sort_photos.py faces --apply --limit 7 --cpu"
    box.log.unlink()
    faces_run(box, lib, hw="auto")
    assert box.calls()[1] == "sort_photos.py faces --apply"


@pytest.mark.parametrize("bad", ["abc", "0", "-5", "1.5"])
def test_faces_ignores_a_limit_that_is_not_a_positive_number(box, folders, bad):
    _, lib = folders
    r = faces_run(box, lib, limit=bad)
    assert "not a number" in r.stderr and box.calls()[1] == "sort_photos.py faces --apply"


def test_faces_extra_options_and_database(box, folders, tmp_path):
    _, lib = folders
    faces_run(box, lib, PHOTO_SORTER_DB=tmp_path / "d.db", PHOTO_SORTER_FACES_ARGS="--det-size 960")
    calls = box.calls()
    assert calls[0] == f"sort_photos.py refresh {lib} --apply --db {tmp_path / 'd.db'}"
    assert calls[1] == f"sort_photos.py faces --apply --db {tmp_path / 'd.db'} --det-size 960"
    assert calls[2] == f"sort_photos.py cluster --apply --db {tmp_path / 'd.db'}"


def test_faces_uses_and_saves_the_library_folder(box, folders):
    _, lib = folders
    faces_run(box, lib)
    assert f"LIBRARY_DIR={lib}" in box.conf_text()
    box.log.unlink()
    r = box.run("photo-sorter.sh", "faces", stdin=answers("", "", "", "y"))
    assert f"[{lib}]" in r.stdout and box.calls()[0] == f"sort_photos.py refresh {lib} --apply"


def test_faces_cancel_and_decline(box, folders):
    _, lib = folders
    assert box.run("photo-sorter.sh", "faces", stdin="q\n").returncode == 0
    r = faces_run(box, lib, go="n")
    assert r.returncode == 0 and box.calls() == [] and "Nothing was changed" in r.stdout


def test_faces_library_folder_must_exist(box, folders):
    _, lib = folders
    r = box.run("photo-sorter.sh", "faces", stdin=answers(box.tmp / "nope", lib, "", "", "y"))
    assert "doesn't exist" in r.stderr and box.commands() == ["refresh", "faces", "cluster"]


def test_faces_missing_packages_declined_does_nothing(box, folders):
    _, lib = folders
    r = faces_run(box, lib, extra=["n"], FAKE_MISSING="insightface")
    assert r.returncode == 1 and box.calls() == [] and "insightface" in r.stderr


def test_faces_missing_packages_accepted_installs_first(box, folders):
    _, lib = folders
    r = faces_run(box, lib, extra=["y"], FAKE_MISSING="onnxruntime")
    assert "pip install -r requirements-faces.txt" in box.calls()[0] and box.commands() == ["refresh", "faces", "cluster"]
    assert "NVIDIA" in r.stdout


def test_faces_stops_when_refresh_cannot_run(box, folders):
    _, lib = folders
    r = faces_run(box, lib, FAKE_RC_refresh_apply=2)
    assert r.returncode == 1 and box.commands() == ["refresh"] and "Stopping" in r.stderr


def test_faces_refresh_with_errors_asks_whether_to_go_on(box, folders):
    _, lib = folders
    r = faces_run(box, lib, extra=["n"], FAKE_RC_refresh_apply=1)
    assert r.returncode == 1 and box.commands() == ["refresh"]
    box.log.unlink()
    r = faces_run(box, lib, extra=["y"], FAKE_RC_refresh_apply=1)
    assert r.returncode == 0 and box.commands() == ["refresh", "faces", "cluster"]


def test_faces_ctrl_c_during_the_scan_stops_and_says_how_to_resume(box, folders):
    _, lib = folders
    r = faces_run(box, lib, FAKE_RC_faces_apply=130)
    assert r.returncode == 130 and box.commands() == ["refresh", "faces"] and "continue where it left off" in r.stderr


def test_faces_fatal_scan_error_skips_grouping(box, folders):
    _, lib = folders
    r = faces_run(box, lib, FAKE_RC_faces_apply=2)
    assert r.returncode == 1 and box.commands() == ["refresh", "faces"]


def test_faces_unreadable_photos_do_not_stop_grouping(box, folders):
    _, lib = folders
    r = faces_run(box, lib, FAKE_RC_faces_apply=1)
    assert r.returncode == 0 and box.commands() == ["refresh", "faces", "cluster"] and "could not be scanned" in r.stderr


def test_faces_cluster_failure_is_reported(box, folders):
    _, lib = folders
    r = faces_run(box, lib, FAKE_RC_cluster_apply=1)
    assert r.returncode == 1 and "Grouping did not finish" in r.stderr


# ===================================================================== photo-sorter.sh: 4) camera details
@pytest.mark.parametrize("word", ["metadata", "4"])
def test_metadata_previews_then_reads(box, word, tmp_path):
    db = tmp_path / "i.db"
    r = box.run("photo-sorter.sh", word, stdin="\ny\n", PHOTO_SORTER_DB=db)
    assert r.returncode == 0 and box.calls() == [f"sort_photos.py metadata --db {db}", f"sort_photos.py metadata --apply --db {db}"]


def test_metadata_reread_decline_and_menu_return(box):
    box.run("photo-sorter.sh", "metadata", stdin="y\n\n")
    assert box.calls() == ["sort_photos.py metadata --reread", "sort_photos.py metadata --apply --reread"]
    box.log.unlink()
    r = box.run("photo-sorter.sh", "metadata", stdin="n\nn\n")
    assert r.returncode == 0 and box.commands() == ["metadata"] and "Nothing was changed" in r.stdout + r.stderr
    box.log.unlink()
    r = box.run("photo-sorter.sh", stdin="4\n\n\n\nq\n")
    assert r.returncode == 0 and box.commands() == ["metadata", "metadata"]


def test_metadata_stop_rules(box):
    r = box.run("photo-sorter.sh", "metadata", stdin="\ny\n", FAKE_RC_metadata=2)
    assert r.returncode == 1 and "no index yet" in r.stderr and box.commands() == ["metadata"]
    for rc, text in ((1, "could not be read"), (130, "Run this option again"), (3, "did not finish")):
        r = box.run("photo-sorter.sh", "metadata", stdin="\ny\n", FAKE_RC_metadata_apply=rc)
        assert r.returncode == rc and text in r.stderr, rc


# ===================================================================== photo-sorter.sh: 5) prepare search
DFN = "ViT-L-14-quickgelu__dfn2b"
SIGLIP = "ViT-B-16-SigLIP2__webli"


@pytest.mark.parametrize("word", ["search", "5"])
def test_search_previews_then_processes(box, word, tmp_path):
    db = tmp_path / "i.db"
    r = box.run("photo-sorter.sh", word, stdin="\ny\n", PHOTO_SORTER_DB=db)
    assert r.returncode == 0 and box.calls() == [f"sort_photos.py embed --model {DFN} --db {db}",
                                                 f"sort_photos.py embed --apply --model {DFN} --db {db}"]


def test_search_model_choice_is_remembered_decline_and_menu_return(box):
    box.run("photo-sorter.sh", "search", stdin=f' "{SIGLIP}" \ny\n')
    assert box.calls() == [f"sort_photos.py embed --model {SIGLIP}", f"sort_photos.py embed --apply --model {SIGLIP}"]
    box.log.unlink()
    r = box.run("photo-sorter.sh", "search", stdin="\nn\n")
    assert r.returncode == 0 and box.calls() == [f"sort_photos.py embed --model {SIGLIP}"]
    assert "Nothing was changed" in r.stdout + r.stderr
    box.log.unlink()
    r = box.run("photo-sorter.sh", stdin="5\n\n\n\nq\n")
    assert r.returncode == 0 and box.commands() == ["embed", "embed"]


def test_search_offers_to_download_a_missing_model(box):
    r = box.run("photo-sorter.sh", "search", stdin="\ny\ny\n", FAKE_RC_embed=3)
    assert r.returncode == 0 and box.calls() == [
        f"sort_photos.py embed --model {DFN}", f"sort_photos.py clipcheck --model {DFN} --download --no-cpu-compare",
        f"sort_photos.py embed --apply --model {DFN}"]
    box.log.unlink()
    r = box.run("photo-sorter.sh", "search", stdin="\nn\n", FAKE_RC_embed=3)
    assert r.returncode == 0 and box.commands() == ["embed"]
    box.log.unlink()
    r = box.run("photo-sorter.sh", "search", stdin="\ny\ny\n", FAKE_RC_embed=3, FAKE_RC_clipcheck_download=1)
    assert r.returncode == 1 and box.commands() == ["embed", "clipcheck"] and "could not be downloaded" in r.stderr
    box.log.unlink()
    r = box.run("photo-sorter.sh", "search", stdin="\ny\ny\n", FAKE_RC_embed=3, FAKE_RC_clipcheck_download=130)
    assert r.returncode == 130 and "fetch the rest" in r.stderr


def test_search_stop_rules(box):
    r = box.run("photo-sorter.sh", "search", stdin="\ny\n", FAKE_RC_embed=2)
    assert r.returncode == 1 and "is there an index yet" in r.stderr and box.commands() == ["embed"]
    for rc, text in ((1, "tried again next time"), (130, "Run this option again"), (4, "did not finish")):
        r = box.run("photo-sorter.sh", "search", stdin="\ny\n", FAKE_RC_embed_apply=rc)
        assert r.returncode == rc and text in r.stderr, rc


def test_search_offers_to_install_tokenizers(box):
    r = box.run("photo-sorter.sh", "search", stdin="n\n", FAKE_MISSING="tokenizers")
    assert r.returncode == 1 and "tokenizers" in r.stderr and box.commands() == []


# ===================================================================== photo-sorter.sh: 6) check a search model
@pytest.mark.parametrize("word", ["clipcheck", "6"])
def test_clipcheck_lists_checks_and_remembers_the_model(box, word, tmp_path):
    db = tmp_path / "i.db"
    r = box.run("photo-sorter.sh", word, stdin="\n", PHOTO_SORTER_DB=db)
    assert r.returncode == 0 and box.calls() == ["sort_photos.py clipcheck --list", f"sort_photos.py clipcheck --model {DFN} --db {db}"]
    assert f"SEARCH_MODEL={DFN}" in box.conf_text()


def test_clipcheck_choice_becomes_the_default_for_option_5(box):
    assert box.run("photo-sorter.sh", "clipcheck", stdin=f"{SIGLIP}\n").returncode == 0
    box.log.unlink()
    box.run("photo-sorter.sh", "search", stdin="\nn\n")
    assert box.calls() == [f"sort_photos.py embed --model {SIGLIP}"]
    box.log.unlink()
    r = box.run("photo-sorter.sh", stdin="6\n\n\nq\n")
    assert r.returncode == 0 and box.commands() == ["clipcheck", "clipcheck"]


def test_clipcheck_downloads_only_after_asking(box):
    r = box.run("photo-sorter.sh", "clipcheck", stdin="\ny\n", FAKE_RC_clipcheck=3)
    assert r.returncode == 0 and box.calls() == ["sort_photos.py clipcheck --list", f"sort_photos.py clipcheck --model {DFN}",
                                                 f"sort_photos.py clipcheck --model {DFN} --download"]
    box.log.unlink()
    r = box.run("photo-sorter.sh", "clipcheck", stdin="\nn\n", FAKE_RC_clipcheck=3)
    assert r.returncode == 0 and not any("--download" in c for c in box.calls())
    for rc, text in ((1, "did not pass the check"), (130, "continues where it stopped"), (2, "could not run")):
        r = box.run("photo-sorter.sh", "clipcheck", stdin="\ny\n", FAKE_RC_clipcheck=3, FAKE_RC_clipcheck_download=rc)
        assert r.returncode == rc and text in r.stderr, rc


def test_clipcheck_failures_are_not_remembered(box):
    for rc, text in ((1, "did not pass the check"), (2, "is the model name right")):
        r = box.run("photo-sorter.sh", "clipcheck", stdin="bad\n", FAKE_RC_clipcheck=rc)
        assert r.returncode == rc and text in r.stderr, rc
        assert "SEARCH_MODEL" not in box.conf_text()


# ===================================================================== run-tests.sh
def pytest_call(box):
    return [c for c in box.calls() if c.startswith("-m pytest")][0]


def test_run_tests_default_skips_the_browser_tests(box):
    r = box.run("run-tests.sh")
    assert r.returncode == 0 and "All tests passed" in r.stdout
    assert pytest_call(box) == "-m pytest tests -m not e2e"


def test_run_tests_all_and_e2e_modes(box):
    box.run("run-tests.sh", "--all")
    assert pytest_call(box) == "-m pytest tests"
    box.log.unlink()
    box.run("run-tests.sh", "--e2e")
    assert pytest_call(box) == "-m pytest tests -m e2e"


def test_run_tests_passes_options_to_pytest(box):
    box.run("run-tests.sh", "-k", "rename", "-x", "-v")
    assert pytest_call(box) == "-m pytest tests -m not e2e -k rename -x -v"
    box.log.unlink()
    box.run("run-tests.sh", "--", "tests/test_sort.py", "--maxfail=2")
    assert pytest_call(box).endswith("tests/test_sort.py --maxfail=2")


def test_run_tests_coverage(box):
    box.run("run-tests.sh", "--cov")
    assert "--cov=." in pytest_call(box) and "--cov-report=term-missing" in pytest_call(box)
    box.log.unlink()
    r = box.run("run-tests.sh", "--cov", FAKE_MISSING="pytest_cov")
    assert "--cov" not in pytest_call(box) and "pytest-cov is not installed" in r.stderr


def test_run_tests_exit_code_follows_pytest(box):
    r = box.run("run-tests.sh", FAKE_PYTEST_RC=1)
    assert r.returncode == 1 and "Tests failed" in r.stderr
    assert box.run("run-tests.sh", FAKE_PYTEST_RC=5).returncode == 5


def test_run_tests_install_option(box):
    box.run("run-tests.sh", "--install")
    assert box.calls()[0] == "-m pip install -r requirements-dev.txt" and not any("playwright" in c for c in box.calls())
    box.log.unlink()
    box.run("run-tests.sh", "--install", "--all")
    assert "-m playwright install chromium" in box.calls()
    box.log.unlink()
    r = box.run("run-tests.sh", "--install", FAKE_PIP_RC=1)
    assert r.returncode == 1 and not any("pytest" in c for c in box.calls())


def test_run_tests_tells_you_when_pytest_is_missing(box):
    r = box.run("run-tests.sh", FAKE_MISSING="pytest")
    assert r.returncode == 1 and "--install" in r.stderr and not any("-m pytest" in c for c in box.calls())


def test_run_tests_unknown_option(box):
    r = box.run("run-tests.sh", "--nonsense")
    assert r.returncode == 2 and "Unknown option" in r.stderr and box.calls() == []


def test_run_tests_static_checks_catch_a_python_syntax_error(box):
    (box.app / "broken.py").write_text("def oops(:\n", encoding="utf-8")
    r = box.run("run-tests.sh", PHOTO_SORTER_PYTHON=sys.executable)
    assert r.returncode == 1 and "broken.py" in r.stdout and "Fix the syntax errors" in r.stderr


def test_run_tests_static_checks_catch_a_shell_syntax_error(box):
    (box.app / "scripts" / "bad.sh").write_text("if true; then\n", encoding="utf-8")
    r = box.run("run-tests.sh", PHOTO_SORTER_PYTHON=sys.executable)
    assert r.returncode == 1 and "bad.sh" in r.stderr


def test_run_tests_no_static_skips_the_syntax_checks(box):
    (box.app / "broken.py").write_text("def oops(:\n", encoding="utf-8")
    r = box.run("run-tests.sh", "--no-static", PHOTO_SORTER_PYTHON=sys.executable, FAKE_PYTEST_RC=0)
    assert "Syntax checks" not in r.stdout


# ===================================================================== deploy.sh
PLACEHOLDER = "/path/to/production/photo-sorter"


def deploy(box, target, *args, stdin="", **env):
    return box.run("deploy.sh", "--target", str(target), "--skip-tests", "--yes", *args, stdin=stdin, **env)


def listing(root: Path):
    return sorted(str(p.relative_to(root)).replace("\\", "/") for p in root.rglob("*") if p.is_file())


def test_deploy_refuses_the_placeholder_and_an_empty_target(box):
    for args in ([], ["--target", PLACEHOLDER], ["--target", ""]):
        r = box.run("deploy.sh", *args, "--skip-tests", "--yes")
        assert r.returncode == 2 and "placeholder" in r.stderr and "--target" in r.stdout
    assert not Path(PLACEHOLDER).exists()


def test_deploy_refuses_a_placeholder_from_the_environment_and_from_deploy_conf(box):
    assert box.run("deploy.sh", "--yes", "--skip-tests", DEPLOY_TARGET=PLACEHOLDER).returncode == 2
    conf = box.tmp / "deploy.conf"
    conf.write_text(f"DEPLOY_TARGET={PLACEHOLDER}\n", encoding="utf-8")
    assert box.run("deploy.sh", "--yes", "--skip-tests", DEPLOY_CONF=conf).returncode == 2


def test_deploy_example_config_ships_with_the_placeholder():
    text = (ROOT / "deploy.conf.example").read_text(encoding="utf-8")
    assert f"DEPLOY_TARGET={PLACEHOLDER}" in text


def test_deploy_target_from_environment_and_from_deploy_conf(box, tmp_path):
    env_target = tmp_path / "from-env"
    assert box.run("deploy.sh", "--yes", "--skip-tests", DEPLOY_TARGET=env_target).returncode == 0 and (env_target / "sort_photos.py").exists()
    conf = tmp_path / "my.conf"
    conf_target = tmp_path / "from-conf"
    conf.write_text(f"# comment\nDEPLOY_TARGET={conf_target}\nKEEP_BACKUPS=3\n", encoding="utf-8")
    assert box.run("deploy.sh", "--yes", "--skip-tests", DEPLOY_CONF=conf).returncode == 0 and (conf_target / "sort_photos.py").exists()


def test_deploy_dry_run_changes_nothing_and_lists_files(box, tmp_path):
    target = tmp_path / "prod"
    r = box.run("deploy.sh", "--target", target, "--dry-run")
    assert r.returncode == 0 and not target.exists() and not Path(f"{target}-backups").exists()
    assert "sort_photos.py" in r.stdout and "Dry run" in r.stdout and "tests would run" in r.stdout
    assert box.calls() == []                          # not even the tests


def test_deploy_copies_only_program_files(box, tmp_path):
    (box.app / "stray.db").write_bytes(b"x")
    (box.app / "deploy.conf").write_text("DEPLOY_TARGET=/secret\n", encoding="utf-8")
    (box.app / "web" / "__pycache__").mkdir()
    (box.app / "web" / "__pycache__" / "junk.pyc").write_bytes(b"x")
    target = tmp_path / "prod"
    r = deploy(box, target)
    assert r.returncode == 0 and "every file matches" in r.stdout
    files = listing(target)
    for wanted in ("sort_photos.py", "face_scan.py", "face_cluster.py", "ui_server.py", "photo_browse.py", "web/app.js", "web/style.css", "README.md",
                   "photo-sorter.sh", "scripts/common.sh", "requirements-ui.txt", "VERSION", ".deployed-files"):
        assert wanted in files, wanted
    for unwanted in ("run-tests.sh", "deploy.sh", "requirements-dev.txt", "stray.db", "deploy.conf", "pytest.ini"):
        assert unwanted not in files
    assert not any(f.startswith("tests/") or f.endswith(".pyc") for f in files)
    version = (target / "VERSION").read_text(encoding="utf-8")
    assert "version=" in version and "deployed_at=" in version
    assert set((target / ".deployed-files").read_text(encoding="utf-8").split()) == set(files)
    assert os.access(target / "photo-sorter.sh", os.X_OK)


def test_deploy_runs_the_start_up_check_with_the_target_python(box, tmp_path):
    r = deploy(box, tmp_path / "prod")
    assert "sort_photos.py starts" in r.stdout and "sort_photos.py --help" in box.calls()[-1]
    r = deploy(box, tmp_path / "prod2", FAKE_RC__help=1)       # the fake only fails for sort_photos.py --help
    assert "sort_photos.py starts" in r.stdout or r.returncode in (0, 1)


def test_deploy_never_touches_data_config_or_venv_in_production(box, tmp_path):
    target = tmp_path / "prod"
    (target / "thumbs").mkdir(parents=True)
    (target / ".venv" / "bin").mkdir(parents=True)
    keep = {"photo_index.db": b"database", ".photo-sorter.conf": b"LAST_SOURCE=x\n", "thumbs/a.jpg": b"t", ".venv/bin/python": b"py", "notes.txt": b"mine"}
    for rel, data in keep.items():
        (target / rel).write_bytes(data)
    assert deploy(box, target).returncode == 0
    assert deploy(box, target).returncode == 0
    for rel, data in keep.items():
        assert (target / rel).read_bytes() == data
    assert not any("photo_index.db" in line or "thumbs" in line for line in (target / ".deployed-files").read_text(encoding="utf-8").splitlines())


def test_deploy_asks_for_confirmation_without_yes(box, tmp_path):
    target = tmp_path / "prod"
    r = box.run("deploy.sh", "--target", target, "--skip-tests", stdin="n\n")
    assert r.returncode == 0 and not target.exists() and "Nothing was changed" in r.stdout
    r = box.run("deploy.sh", "--target", target, "--skip-tests", stdin="y\n")
    assert r.returncode == 0 and (target / "sort_photos.py").exists()


def test_first_deployment_makes_no_backup(box, tmp_path):
    target = tmp_path / "prod"
    r = deploy(box, target)
    assert "first deployment" in r.stdout and not Path(f"{target}-backups").exists()


def test_redeploy_backs_up_what_it_replaces(box, tmp_path):
    target = tmp_path / "prod"
    deploy(box, target)
    original = (target / "sort_photos.py").read_text(encoding="utf-8")
    (box.app / "sort_photos.py").write_text(original + "\n# version two\n", encoding="utf-8")
    r = deploy(box, target)
    assert r.returncode == 0 and "version two" in (target / "sort_photos.py").read_text(encoding="utf-8")
    backups = sorted(p for p in Path(f"{target}-backups").iterdir())
    assert len(backups) == 1 and (backups[0] / "sort_photos.py").read_text(encoding="utf-8") == original


def test_deploy_keeps_only_the_newest_backups(box, tmp_path):
    target = tmp_path / "prod"
    deploy(box, target)
    backups = Path(f"{target}-backups")
    for name in ("20200101-000000", "20210101-000000", "20220101-000000", "20230101-000000"):
        (backups / name).mkdir(parents=True)
        (backups / name / "marker").write_text(name, encoding="utf-8")
    (box.app / "README.md").write_text("changed\n", encoding="utf-8")
    deploy(box, target, "--keep", "2")
    left = sorted(p.name for p in backups.iterdir())
    assert len(left) == 2 and left[0] == "20230101-000000" and left[1] > "2024"


def test_deploy_keep_comes_from_the_config_too(box, tmp_path):
    target = tmp_path / "prod"
    deploy(box, target)
    backups = Path(f"{target}-backups")
    for name in ("20200101-000000", "20210101-000000", "20220101-000000"):
        (backups / name).mkdir(parents=True)
    (box.app / "README.md").write_text("again\n", encoding="utf-8")
    deploy(box, target, KEEP_BACKUPS=1)
    assert len(list(backups.iterdir())) == 1


def test_deploy_no_backup(box, tmp_path):
    target = tmp_path / "prod"
    deploy(box, target)
    (box.app / "README.md").write_text("new\n", encoding="utf-8")
    r = deploy(box, target, "--no-backup")
    assert "No backup" in r.stderr and not Path(f"{target}-backups").exists()


def test_deploy_removes_files_that_are_no_longer_part_of_the_program(box, tmp_path):
    target = tmp_path / "prod"
    (box.app / "old_module.py").write_text("x = 1\n", encoding="utf-8")
    deploy(box, target)
    assert (target / "old_module.py").exists()
    (target / "mine.txt").write_text("user file", encoding="utf-8")
    (box.app / "old_module.py").unlink()
    r = deploy(box, target)
    assert not (target / "old_module.py").exists() and (target / "mine.txt").exists() and "1 old file(s) removed" in r.stdout
    assert any((p / "old_module.py").exists() for p in Path(f"{target}-backups").iterdir())


def test_deploy_validates_options(box, tmp_path):
    assert box.run("deploy.sh", "--bogus").returncode == 2
    r = box.run("deploy.sh", "--target", tmp_path / "p", "--keep", "many", "--skip-tests", "--yes")
    assert r.returncode == 1 and "number" in r.stderr and not (tmp_path / "p").exists()


def test_deploy_runs_the_tests_first_and_stops_when_they_fail(box, tmp_path):
    target = tmp_path / "prod"
    r = box.run("deploy.sh", "--target", target, "--yes", FAKE_PYTEST_RC=1)
    assert r.returncode == 1 and "nothing was deployed" in r.stderr and not target.exists()
    r = box.run("deploy.sh", "--target", target, "--yes")
    assert r.returncode == 0 and any(c.startswith("-m pytest") for c in box.calls()) and (target / "sort_photos.py").exists()


def test_deploy_skip_tests_warns(box, tmp_path):
    r = deploy(box, tmp_path / "prod")
    assert "Skipping the tests" in r.stderr and not any(c.startswith("-m pytest") for c in box.calls())


def test_deploy_target_with_spaces_and_quotes(box, tmp_path):
    target = tmp_path / "my production folder"
    r = box.run("deploy.sh", "--target", f'"{target}"', "--skip-tests", "--yes")
    assert r.returncode == 0 and (target / "sort_photos.py").exists()


def test_rollback_restores_the_previous_version(box, tmp_path):
    target = tmp_path / "prod"
    deploy(box, target)
    v1 = (target / "sort_photos.py").read_text(encoding="utf-8")
    (box.app / "sort_photos.py").write_text(v1 + "\n# v2\n", encoding="utf-8")
    (box.app / "new_in_v2.py").write_text("x = 2\n", encoding="utf-8")
    deploy(box, target)
    assert (target / "new_in_v2.py").exists()
    (target / "photo_index.db").write_bytes(b"data")
    r = box.run("deploy.sh", "--target", target, "--rollback", "--yes")
    assert r.returncode == 0 and "Rolled back" in r.stdout
    assert (target / "sort_photos.py").read_text(encoding="utf-8") == v1
    assert not (target / "new_in_v2.py").exists() and (target / "photo_index.db").read_bytes() == b"data"
    assert any(Path(f"{target}-backups").iterdir())             # the backup itself is kept


def test_rollback_without_a_backup(box, tmp_path):
    r = box.run("deploy.sh", "--target", tmp_path / "prod", "--rollback", "--yes")
    assert r.returncode == 1 and "no backup" in r.stderr.lower()


def test_rollback_asks_first_and_honours_dry_run(box, tmp_path):
    target = tmp_path / "prod"
    deploy(box, target)
    (box.app / "README.md").write_text("v2\n", encoding="utf-8")
    deploy(box, target)
    r = box.run("deploy.sh", "--target", target, "--rollback", stdin="n\n")
    assert r.returncode == 0 and (target / "README.md").read_text(encoding="utf-8") == "v2\n"
    r = box.run("deploy.sh", "--target", target, "--rollback", "--dry-run", "--yes")
    assert "Dry run" in r.stdout and (target / "README.md").read_text(encoding="utf-8") == "v2\n"


def test_rollback_is_refused_for_the_placeholder_target(box):
    assert box.run("deploy.sh", "--target", PLACEHOLDER, "--rollback", "--yes").returncode == 2
