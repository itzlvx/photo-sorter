"""Phase 7.5: the Tasks page API — the folder browser, and running sort / faces / search as background jobs."""
import json
import shutil
import sqlite3
import sys
import time
from pathlib import Path

import pytest

from conftest import make_photo, scalar, tree

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

import sort_photos as sp  # noqa: E402
import ui_tasks  # noqa: E402

HEADERS = {"X-Photo-Sorter": "1"}

FAKE = r'''
import json, os, signal, sys, time
if hasattr(signal, "SIGBREAK"):
    signal.signal(signal.SIGBREAK, signal.default_int_handler)
cmd = sys.argv[1]
with open(os.environ["FAKE_LOG"], "a", encoding="utf-8") as f:
    f.write(json.dumps(sys.argv[1:]) + "\n")
print("fake " + cmd + " running", flush=True)
if os.environ.get("FAKE_WAIT") == cmd:
    try:
        while True:
            time.sleep(0.05)
    except KeyboardInterrupt:
        print("saved, stopping", flush=True)
        sys.exit(130)
sys.exit(int(os.environ.get("FAKE_RC_" + cmd, "0")))
'''


class Tasks:
    def __init__(self, app, client, env, log=None):
        self.app, self.client, self.env, self.log = app, client, env, log

    def post(self, path, body=None):
        return self.client.post(path, json=body or {}, headers=HEADERS)

    def wait(self, timeout=90):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            job = self.client.get("/api/jobs/current").json()["job"]
            if job and job["state"] != "running":
                return job
            time.sleep(0.05)
        raise AssertionError("the job did not finish in time")

    def calls(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]


def make_app(env, **kwargs):
    import ui_server
    from fastapi.testclient import TestClient
    env.db.parent.mkdir(parents=True, exist_ok=True)
    sp.open_index(env.db).close()
    app = ui_server.create_app(env.db, **kwargs)
    return app, TestClient(app, base_url="http://127.0.0.1:8765")


@pytest.fixture
def real(env):
    """Jobs run the real sort_photos.py."""
    app, client = make_app(env)
    yield Tasks(app, client, env)
    app.state.jobs.shutdown()


@pytest.fixture
def fake(env, tmp_path, monkeypatch):
    """Jobs run a fake command that records its arguments; FAKE_RC_<cmd> and FAKE_WAIT steer it."""
    script = tmp_path / "fake_sort.py"
    script.write_text(FAKE, encoding="utf-8")
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("FAKE_LOG", str(log))
    app, client = make_app(env, job_command=[sys.executable, "-u", str(script)])
    yield Tasks(app, client, env, log)
    app.state.jobs.shutdown()


# --------------------------------------------------------------------------- folder browser
def test_folders_are_listed_sorted_without_hidden_ones(real, tmp_path):
    for name in ("beta", "Alpha", ".hidden", "$RECYCLE.BIN", "gamma"):
        (tmp_path / "pick" / name).mkdir(parents=True)
    (tmp_path / "pick" / "file.jpg").write_bytes(b"x")
    data = real.client.get("/api/folders", params={"path": str(tmp_path / "pick")}).json()
    assert [f["name"] for f in data["folders"]] == ["Alpha", "beta", "gamma"]
    assert data["path"] == str(tmp_path / "pick") and data["parent"] == str(tmp_path)
    assert data["folders"][0]["path"] == str(tmp_path / "pick" / "Alpha")
    labels = [s["label"] for s in data["shortcuts"]]
    assert "Home" in labels and len(data["shortcuts"]) >= 2   # home plus at least one drive or /


def test_folder_browser_starts_at_the_shortcuts_and_explains_bad_paths(real, tmp_path):
    start = real.client.get("/api/folders").json()
    assert start["path"] == "" and start["folders"] == [] and start["shortcuts"]
    assert real.client.get("/api/folders", params={"path": str(tmp_path / "nope")}).status_code == 404
    assert real.client.get("/api/folders", params={"path": "relative/folder"}).status_code == 400
    quoted = real.client.get("/api/folders", params={"path": f'"{tmp_path}"'}).json()   # pasted from Explorer
    assert quoted["path"] == str(tmp_path)


def test_remembered_folders_become_shortcuts(real, env):
    env.dest.mkdir()
    ui_tasks.conf_set("LIBRARY_DIR", str(env.dest))
    shortcuts = real.client.get("/api/folders").json()["shortcuts"]
    assert {"label": "Library", "path": str(env.dest)} in shortcuts


# --------------------------------------------------------------------------- sorting (the real command)
def test_sort_preview_changes_nothing_then_sorting_copies_and_indexes(real, env):
    for i in range(3):
        make_photo(env.src / f"p{i}.jpg", seed=i, taken=f"2021:0{i + 1}:05 10:00:00")
    r = real.post("/api/tasks/sort", {"source": str(env.src), "library": str(env.dest)})
    assert r.status_code == 200 and r.json()["state"] == "running" and r.json()["apply"] is False
    job = real.wait()
    assert job["state"] == "finished" and job["rc"] == 0 and job["params"] == {"source": str(env.src), "library": str(env.dest), "move": False}
    text = "\n".join(job["lines"])
    assert "Would sort:" in text and "Dry run only" in text and not env.dest.exists()
    assert scalar(env.db, "SELECT COUNT(*) FROM photos") == 0

    version = real.app.state.version
    assert real.post("/api/tasks/sort", {"source": str(env.src), "library": str(env.dest), "apply": True}).status_code == 200
    job = real.wait()
    assert job["state"] == "finished" and "Photos sorted:" in "\n".join(job["lines"])
    assert tree(env.dest) == ["2021/01-jan/p0.jpg", "2021/02-feb/p1.jpg", "2021/03-mar/p2.jpg"]
    assert len(tree(env.src)) == 3                                    # copied, not moved
    assert real.client.get("/api/summary").json()["photos"] == 3      # the page sees the new photos
    assert real.app.state.version > version
    assert ui_tasks.conf_get("LAST_SOURCE") == str(env.src) and ui_tasks.conf_get("LIBRARY_DIR") == str(env.dest)


def test_sort_can_move(real, env):
    make_photo(env.src / "a.jpg", seed=1, taken="2020:05:01 08:00:00")
    real.post("/api/tasks/sort", {"source": str(env.src), "library": str(env.dest), "move": True, "apply": True})
    job = real.wait()
    assert job["state"] == "finished" and "move" in job["title"]
    assert tree(env.src) == [] and tree(env.dest) == ["2020/05-may/a.jpg"]


@pytest.mark.parametrize("source, library, message", [
    ("", "LIB", "Choose the folder with the new photos"),
    ("SRC", "", "Choose the library folder"),
    ("relative", "LIB", "full path"),
    ("MISSING", "LIB", "does not exist"),
    ("SRC", "SRC", "different folders"),
])
def test_sort_folders_are_checked_first(real, env, source, library, message):
    names = {"SRC": str(env.src), "LIB": str(env.dest), "MISSING": str(env.tmp / "missing")}
    r = real.post("/api/tasks/sort", {"source": names.get(source, source), "library": names.get(library, library)})
    assert r.status_code == 400 and message in r.json()["detail"]
    assert real.client.get("/api/jobs/current").json()["job"] is None


def test_a_stopped_sort_keeps_what_is_finished_and_leaves_no_partial_copy(env, cli, monkeypatch):
    for i in range(3):
        make_photo(env.src / f"p{i}.jpg", seed=i, taken="2021:01:05 10:00:00")
    real_copy = sp.shutil.copy2
    copies = []

    def copy_then_stop(src, dst, *args, **kwargs):
        copies.append(dst)
        if len(copies) == 2:
            Path(dst).write_bytes(b"half a photo")
            raise KeyboardInterrupt
        return real_copy(src, dst, *args, **kwargs)

    monkeypatch.setattr(sp.shutil, "copy2", copy_then_stop)
    r = cli("sort", env.src, env.dest, "--apply", "--move")
    assert r.code == 130 and "Stopped by you" in r.err and "--- Summary ---" in r.out
    assert len(tree(env.dest)) == 1 and not Path(copies[1]).exists()   # the half-written copy is gone
    assert len(tree(env.src)) == 2                                        # the interrupted original is still there
    assert scalar(env.db, "SELECT COUNT(*) FROM photos") == 1


# --------------------------------------------------------------------------- one job at a time, stopping
def test_only_one_task_runs_at_a_time_and_stop_skips_the_rest(fake, env, monkeypatch):
    monkeypatch.setattr(ui_tasks, "STOP_GRACE", 3.0)
    monkeypatch.setenv("FAKE_WAIT", "faces")
    env.dest.mkdir()
    assert fake.post("/api/tasks/faces", {"library": str(env.dest)}).status_code == 200
    end = time.monotonic() + 30
    while "fake faces running" not in fake.client.get("/api/jobs/current").json()["job"]["lines"]:
        assert time.monotonic() < end
        time.sleep(0.05)
    busy = fake.post("/api/tasks/group")
    assert busy.status_code == 409 and "still running" in busy.json()["detail"]
    assert fake.post("/api/regroup").status_code == 409
    assert fake.post("/api/jobs/current/stop").json()["job"]["state"] == "running"
    job = fake.wait()
    assert job["state"] == "stopped" and [s["state"] for s in job["steps"]] == ["ok", "stopped", "skipped"]
    assert [c[0] for c in fake.calls()] == ["refresh", "faces"]
    assert job["lines"][-1].startswith("Stopped.") and "saved, stopping" in job["lines"]   # it was asked, not killed
    assert fake.post("/api/tasks/group").status_code == 200   # free again
    assert fake.wait()["state"] == "finished"


def test_faces_runs_refresh_scan_and_grouping_with_the_chosen_options(fake, env):
    env.dest.mkdir()
    r = fake.post("/api/tasks/faces", {"library": str(env.dest), "limit": 5, "hardware": "gpu"})
    assert r.status_code == 200
    job = fake.wait()
    db = ["--db", str(env.db)]
    assert fake.calls() == [["refresh", str(env.dest), "--apply", *db], ["faces", "--apply", "--limit", "5", "--gpu", *db], ["cluster", "--apply", *db]]
    assert job["state"] == "finished" and [s["state"] for s in job["steps"]] == ["ok", "ok", "ok"]
    assert "=== Step 2 of 3: Find faces ===" in job["lines"]
    assert ui_tasks.conf_get("LIBRARY_DIR") == str(env.dest)


def test_faces_goes_on_after_some_problems_but_not_after_a_failure(fake, env, monkeypatch):
    env.dest.mkdir()
    monkeypatch.setenv("FAKE_RC_faces", "1")
    fake.post("/api/tasks/faces", {"library": str(env.dest), "hardware": "cpu"})
    job = fake.wait()
    assert job["state"] == "problems" and [s["state"] for s in job["steps"]] == ["ok", "problems", "ok"]
    assert fake.calls()[1] == ["faces", "--apply", "--cpu", "--db", str(env.db)]
    monkeypatch.setenv("FAKE_RC_refresh", "2")
    fake.post("/api/tasks/faces", {"library": str(env.dest)})
    job = fake.wait()
    assert job["state"] == "failed" and [s["state"] for s in job["steps"]] == ["failed", "skipped", "skipped"]


def test_faces_needs_an_existing_library_and_a_sensible_limit(fake, env):
    assert fake.post("/api/tasks/faces", {"library": str(env.tmp / "missing")}).status_code == 400
    env.dest.mkdir()
    assert fake.post("/api/tasks/faces", {"library": str(env.dest), "limit": 0}).status_code == 422


def test_the_job_log_keeps_the_newest_lines_and_reads_on_from_where_the_page_was(monkeypatch):
    monkeypatch.setattr(ui_tasks, "MAX_LINES", 5)
    job = ui_tasks.Job(1, "sort", "t", [], {}, False)
    for i in range(3):
        job.write(f"line {i}")
    first = job.json()
    assert first["lines"] == ["line 0", "line 1", "line 2"] and first["next"] == 3 and not first["dropped"]
    for i in range(3, 9):
        job.write(f"line {i}")
    later = job.json(after=first["next"])
    assert later["lines"] == ["line 4", "line 5", "line 6", "line 7", "line 8"] and later["dropped"] and later["next"] == 9
    assert job.json(after=9)["lines"] == []


# --------------------------------------------------------------------------- search vectors
def test_search_preview_download_and_processing_steps(fake, env):
    clip_search = pytest.importorskip("clip_search")
    model = "ViT-B-32__openai"
    r = fake.post("/api/tasks/search", {"model": model})
    assert r.status_code == 200 and r.json()["params"] == {"model": model, "downloaded": False}
    fake.wait()
    assert fake.calls()[-1] == ["embed", "--model", model, "--db", str(env.db)]

    r = fake.post("/api/tasks/search", {"model": model, "apply": True})
    assert r.status_code == 409 and "not downloaded" in r.json()["detail"] and clip_search.size_hint(model) in r.json()["detail"]

    fake.post("/api/tasks/search", {"model": model, "apply": True, "download": True})
    job = fake.wait()
    assert [c[0] for c in fake.calls()[-2:]] == ["clipcheck", "embed"]
    assert fake.calls()[-2] == ["clipcheck", "--model", model, "--download", "--no-cpu-compare", "--db", str(env.db)]
    assert fake.calls()[-1] == ["embed", "--apply", "--model", model, "--db", str(env.db)]
    assert job["state"] == "finished" and ui_tasks.conf_get("SEARCH_MODEL") == model


def test_a_failed_model_check_stops_before_processing(fake, monkeypatch):
    pytest.importorskip("clip_search")
    monkeypatch.setenv("FAKE_RC_clipcheck", "1")
    fake.post("/api/tasks/search", {"model": "ViT-B-32__openai", "apply": True, "download": True})
    job = fake.wait()
    assert job["state"] == "problems" and [s["state"] for s in job["steps"]] == ["problems", "skipped"]


def test_a_downloaded_model_is_listed_and_processed_without_downloading(fake, env):
    clip_search = pytest.importorskip("clip_search")
    from clip_fakes import make_model
    make_model(clip_search.default_model_root() / "test-model")
    info = fake.client.get("/api/tasks").json()
    models = {m["name"]: m for m in info["search"]["models"]}
    assert models["test-model"]["downloaded"] and not models["ViT-B-32__openai"]["downloaded"]
    assert models["ViT-B-32__openai"]["size"] == "0.6 GB" and info["search"]["default"] == clip_search.DEFAULT_MODEL
    fake.post("/api/tasks/search", {"model": "test-model", "apply": True})
    job = fake.wait()
    assert [s["label"] for s in job["steps"]] == ["Store search vectors"] and [c[0] for c in fake.calls()] == ["embed"]
    assert fake.client.get("/api/tasks").json()["search"]["default"] == "test-model"


def test_a_bad_model_name_is_refused(fake):
    pytest.importorskip("clip_search")
    r = fake.post("/api/tasks/search", {"model": "../../etc"})
    assert r.status_code == 400 and "not a valid model name" in r.json()["detail"]


def test_task_info_reports_folders_packages_and_the_last_job(fake, env):
    env.dest.mkdir()
    ui_tasks.conf_set("LAST_SOURCE", str(env.src))
    ui_tasks.conf_set("LIBRARY_DIR", str(env.dest))
    info = fake.client.get("/api/tasks").json()
    assert info["folders"] == {"source": str(env.src), "library": str(env.dest)} and info["job"] is None
    assert set(info["packages"]) == {"faces", "search"}
    fake.post("/api/tasks/group")
    fake.wait()
    assert fake.client.get("/api/tasks").json()["job"]["kind"] == "group"


def test_task_changes_need_the_custom_header(fake):
    assert fake.client.post("/api/tasks/group", json={}).status_code == 403
    assert fake.client.post("/api/jobs/current/stop", json={}).status_code == 403
    assert fake.calls() == []


# --------------------------------------------------------------------------- the photo library (root folder)
def sorted_library(root, seeds, year=2018):
    for i, seed in enumerate(seeds):
        make_photo(root / str(year) / "06-jun" / f"s{seed}.jpg", seed=seed, taken=f"{year}:06:{i + 1:02d} 10:00:00")
    return root


def test_no_library_yet_then_choosing_one(real, env, tmp_path):
    status = real.client.get("/api/library/root").json()
    assert status["root"] == "" and not status["exists"] and status["photos"] == 0
    assert real.post("/api/library/root", {"path": "relative"}).status_code == 400
    later = tmp_path / "not-there-yet"
    status = real.post("/api/library/root", {"path": str(later)}).json()   # a new library that sorting will create
    assert status["root"] == str(later) and not status["exists"]
    assert ui_tasks.conf_get("LIBRARY_DIR") == str(later)
    r = real.post("/api/tasks/library", {})
    assert r.status_code == 400 and "does not exist" in r.json()["detail"]
    assert real.client.get("/api/tasks").json()["library"]["root"] == str(later)


def test_adding_an_already_sorted_library_indexes_it_after_a_preview(real, env):
    sorted_library(env.dest, [1, 2, 3])
    real.post("/api/library/root", {"path": str(env.dest)})
    r = real.post("/api/tasks/library", {"apply": False})
    assert r.status_code == 200 and r.json()["title"].startswith("Preview")
    job = real.wait()
    assert job["state"] == "finished" and "Would add:" in "\n".join(job["lines"])
    assert scalar(env.db, "SELECT COUNT(*) FROM photos") == 0

    real.post("/api/tasks/library", {"faces": False, "search": False})
    job = real.wait()
    assert job["state"] == "finished" and [s["label"] for s in job["steps"]] == ["Add the photos to the index"]
    status = real.client.get("/api/library/root").json()
    assert status["photos"] == 3 and status["outside"] == 0 and status["scanned"] == 0 and status["exists"]
    assert real.client.get("/api/summary").json()["photos"] == 3


def test_switching_to_another_library_keeps_the_old_photos_and_takes_over_copies(real, env, tmp_path):
    sorted_library(env.dest, [1, 2])
    real.post("/api/library/root", {"path": str(env.dest)})
    real.post("/api/tasks/library", {"faces": False, "search": False})
    real.wait()
    with sqlite3.connect(env.db) as conn:
        old_ids = [r for r, in conn.execute("SELECT id FROM photos")]

    other = tmp_path / "other-drive"
    sorted_library(other, [7, 8, 9], year=2021)
    shutil.copy2(env.dest / "2018" / "06-jun" / "s1.jpg", other / "2021" / "06-jun" / "s1.jpg")   # one photo in both
    status = real.post("/api/library/root", {"path": str(other)}).json()
    assert status["photos"] == 0 and status["outside"] == 2              # nothing changes until it is added
    real.post("/api/tasks/library", {"faces": False, "search": False})
    job = real.wait()
    assert job["state"] == "finished"
    status = real.client.get("/api/library/root").json()
    assert status["photos"] == 4 and status["outside"] == 1             # s1 now lives in the new library
    assert scalar(env.db, "SELECT COUNT(*) FROM photos") == 5            # the old library's other photo stays
    assert scalar(env.db, "SELECT id FROM photos WHERE sorted_path LIKE '%other-drive%s1.jpg'") in old_ids
    assert len(tree(env.dest)) == 2                                      # files of the old library are untouched


def test_the_library_task_runs_index_faces_grouping_and_search(fake, env):
    clip_search = pytest.importorskip("clip_search")
    from clip_fakes import make_model
    make_model(clip_search.default_model_root() / "test-model")
    env.dest.mkdir()
    fake.post("/api/library/root", {"path": str(env.dest)})
    r = fake.post("/api/tasks/library", {"model": "test-model", "hardware": "gpu"})
    assert r.status_code == 200
    job = fake.wait()
    db = ["--db", str(env.db)]
    assert fake.calls() == [["refresh", str(env.dest), "--apply", "--adopt", *db], ["faces", "--apply", "--gpu", *db],
                            ["cluster", "--apply", *db], ["embed", "--apply", "--model", "test-model", *db]]
    assert job["state"] == "finished" and job["params"]["model"] == "test-model"
    assert ui_tasks.conf_get("SEARCH_MODEL") == "test-model"


def test_library_task_options_and_failures(fake, env, monkeypatch):
    pytest.importorskip("clip_search")
    env.dest.mkdir()
    fake.post("/api/library/root", {"path": str(env.dest)})
    r = fake.post("/api/tasks/library", {"faces": False, "model": "ViT-B-32__openai"})
    assert r.status_code == 409 and "not downloaded" in r.json()["detail"]      # a download needs a yes first
    fake.post("/api/tasks/library", {"faces": False, "model": "ViT-B-32__openai", "download": True})
    fake.wait()
    assert [c[0] for c in fake.calls()] == ["refresh", "clipcheck", "embed"]

    monkeypatch.setenv("FAKE_RC_cluster", "2")      # e.g. no faces at all: search is still prepared
    fake.post("/api/tasks/library", {"model": "ViT-B-32__openai", "download": True})
    job = fake.wait()
    assert [s["state"] for s in job["steps"]][-3:] == ["failed", "ok", "ok"] and job["state"] == "failed"

    monkeypatch.setenv("FAKE_RC_refresh", "2")      # the index could not be updated: nothing else runs
    fake.post("/api/tasks/library", {"search": False})
    job = fake.wait()
    assert [s["state"] for s in job["steps"]] == ["failed", "skipped", "skipped"]


def test_the_library_cannot_change_while_a_task_runs(fake, env, monkeypatch):
    monkeypatch.setenv("FAKE_WAIT", "cluster")
    fake.post("/api/tasks/group")
    end = time.monotonic() + 30
    while "fake cluster running" not in fake.client.get("/api/jobs/current").json()["job"]["lines"]:
        assert time.monotonic() < end
        time.sleep(0.05)
    r = fake.post("/api/library/root", {"path": str(env.tmp)})
    assert r.status_code == 409 and "still running" in r.json()["detail"]
    fake.post("/api/jobs/current/stop")
    assert fake.wait()["state"] == "stopped"
