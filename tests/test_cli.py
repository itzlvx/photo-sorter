"""Command line behaviour: help text, dispatch, error messages and exit codes. Also runs the real script in a subprocess."""
import subprocess
import sys

import pytest

import sort_photos as sp
from conftest import ROOT

COMMANDS = ["sort", "refresh", "metadata", "faces", "gpucheck", "clipcheck", "embed", "cluster", "ui", "info"]


@pytest.mark.parametrize("command", COMMANDS)
def test_every_command_has_help(command):
    done = subprocess.run([sys.executable, str(ROOT / "sort_photos.py"), command, "--help"], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0 and "usage:" in done.stdout


def test_top_level_help_lists_all_commands():
    done = subprocess.run([sys.executable, str(ROOT / "sort_photos.py"), "--help"], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0 and all(word in done.stdout for word in COMMANDS)


def test_unknown_option_exits_with_usage_error():
    done = subprocess.run([sys.executable, str(ROOT / "sort_photos.py"), "sort", "--nonsense"], capture_output=True, text=True, timeout=60)
    assert done.returncode == 2 and ("unrecognized arguments" in done.stderr or "required" in done.stderr)


def test_sort_needs_both_folders():
    done = subprocess.run([sys.executable, str(ROOT / "sort_photos.py"), "sort", "onlyone"], capture_output=True, text=True, timeout=60)
    assert done.returncode == 2


def test_bad_month_format_is_rejected(env):
    done = subprocess.run([sys.executable, str(ROOT / "sort_photos.py"), "sort", str(env.src), str(env.dest), "--month-format", "weird"],
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 2 and "invalid choice" in done.stderr


def test_full_workflow_through_the_real_script(env):
    """sort -> info works end to end as a separate process, the way the shell menu runs it."""
    from conftest import make_photo
    from datetime import datetime
    make_photo(env.src / "a.jpg", 1, taken=datetime(2019, 3, 15))
    run = lambda *a: subprocess.run([sys.executable, str(ROOT / "sort_photos.py"), *a, "--db", str(env.db)], capture_output=True, text=True, timeout=120)
    assert run("sort", str(env.src), str(env.dest), "--apply").returncode == 0
    done = run("info")
    assert done.returncode == 0 and "Photos indexed: 1" in done.stdout
    assert (env.dest / "2019" / "03-mar" / "a.jpg").exists()


def test_verbose_mode_adds_timestamps_and_debug_lines(env, cli):
    from conftest import make_photo
    make_photo(env.src / "a.jpg", 1)
    plain = cli("sort", env.src, env.dest, "--apply")
    verbose = cli("sort", env.src, env.dest, "--apply", "-v")
    assert "Hashed" not in plain.out and "Hashed" in verbose.out


def test_warnings_go_to_stderr_and_progress_to_stdout(env, cli):
    r = cli("sort", env.tmp / "nope", env.dest)
    assert "ERROR" in r.err and "ERROR" not in r.out


def test_ui_command_explains_missing_packages(env, cli, monkeypatch):
    monkeypatch.setitem(sys.modules, "ui_server", None)       # makes 'import ui_server' raise ImportError
    r = cli("ui")
    assert r.code == 2 and "requirements-ui.txt" in r.err


def test_cluster_without_an_index_says_so(env, cli):
    r = cli("cluster", "--apply")
    assert r.code == 2 and "no index" in r.text.lower()


def test_faces_without_the_packages_explains_how_to_install(env, cli, monkeypatch):
    monkeypatch.setitem(sys.modules, "face_scan", None)
    r = cli("faces")
    assert r.code == 2 and "requirements-faces.txt" in r.err
