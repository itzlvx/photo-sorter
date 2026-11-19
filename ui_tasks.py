"""Phase 7.5: run the photo sorter's own commands from the web interface.

The Tasks page does what the menu (photo-sorter.bat / photo-sorter.sh) does:
  * sort new photos from a source folder into the library (preview first, then copy or move)
  * refresh the index, find faces and group them into people (or only group them)
  * store search vectors (downloading the search model first when you agree)

Each task runs `sort_photos.py <command>` as a separate process, exactly as typed in a terminal,
and its messages are shown live on the page. Only one task runs at a time, because they all
write to the same index. Stop asks the command to finish the photo it is on and save, the same
as pressing Ctrl+C in a terminal.

The folder browser lists the folders on this computer so you can choose them with a click. Like the
rest of the interface it only answers requests for this computer itself (see ui_server.py).
"""
from __future__ import annotations

import ctypes
import os
import signal
import string
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

APP_DIR = Path(__file__).resolve().parent
SCRIPT = APP_DIR / "sort_photos.py"
MAX_LINES = 4000              # the newest lines of a task are kept (a preview can list every photo)
MAX_FOLDERS = 2000
STOP_GRACE = 20.0             # seconds a stopped command gets to save its work before it is ended


# --------------------------------------------------------------------------
# Remembered answers (the same KEY=value file the menu uses)
# --------------------------------------------------------------------------
def conf_file() -> Path:
    return Path(os.environ.get("PHOTO_SORTER_CONF") or APP_DIR / ".photo-sorter.conf")


def conf_get(key: str) -> str:
    value = ""
    try:
        for line in conf_file().read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith(key + "="):
                value = line[len(key) + 1:]
    except OSError:
        pass
    return value


def conf_set(key: str, value: str) -> None:
    path = conf_file()
    try:
        lines = [l for l in path.read_text(encoding="utf-8", errors="replace").splitlines() if not l.startswith(key + "=")]
    except OSError:
        lines = []
    lines.append(f"{key}={value}")
    try:
        path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    except OSError:
        pass  # remembering is a convenience only


# --------------------------------------------------------------------------
# Folders
# --------------------------------------------------------------------------
def clean_folder(text: str | None) -> str:
    """Trim spaces and the quotes that copying a path in Windows Explorer adds; expand ~."""
    text = (text or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1].strip()
    return os.path.expanduser(text) if text.startswith("~") else text


def absolute_folder(text: str | None, label: str, must_exist: bool) -> Path:
    raw = clean_folder(text)
    if not raw:
        raise HTTPException(400, f"Choose the {label}.")
    path = Path(raw)
    if not path.is_absolute():
        example = "D:\\Photos" if os.name == "nt" else "/home/you/Photos"
        raise HTTPException(400, f"The {label} must be a full path, for example {example}.")
    if must_exist and not path.is_dir():
        raise HTTPException(400, f"The {label} does not exist: {path}")
    if not must_exist and path.exists() and not path.is_dir():
        raise HTTPException(400, f"The {label} is a file, not a folder: {path}")
    return path


def drive_roots() -> list[str]:
    if os.name != "nt":
        return ["/"]
    mask = ctypes.windll.kernel32.GetLogicalDrives()  # instant, even for a disconnected network drive
    return [f"{letter}:\\" for i, letter in enumerate(string.ascii_uppercase) if mask >> i & 1]


def shortcuts() -> list[dict]:
    home = Path.home()
    out = [{"label": "Home", "path": str(home)}]
    pictures = home / "Pictures"
    if pictures.is_dir():
        out.append({"label": "Pictures", "path": str(pictures)})
    for key, label in (("LIBRARY_DIR", "Library"), ("LAST_SOURCE", "Last source")):
        value = conf_get(key)
        if value and Path(value).is_dir():
            out.append({"label": label, "path": value})
    out += [{"label": root, "path": root} for root in drive_roots()]
    return out


def list_folders(text: str) -> dict:
    raw = clean_folder(text)
    if not raw:
        return {"path": "", "parent": None, "folders": [], "shortcuts": shortcuts(), "truncated": False}
    path = Path(raw)
    if not path.is_absolute():
        raise HTTPException(400, "Type a full path.")
    if not path.is_dir():
        raise HTTPException(404, f"That folder does not exist: {path}")
    folders = []
    try:
        with os.scandir(path) as entries:
            for entry in entries:
                if entry.name.startswith((".", "$")) or entry.name in ("System Volume Information",):
                    continue
                try:
                    if entry.is_dir():
                        folders.append(entry.name)
                except OSError:
                    continue
    except PermissionError:
        raise HTTPException(403, f"This folder cannot be opened (no permission): {path}")
    except OSError as error:
        raise HTTPException(400, f"This folder cannot be read: {error}")
    folders.sort(key=str.casefold)
    parent = path.parent if path.parent != path else None
    return {
        "path": str(path),
        "parent": str(parent) if parent else "",
        "folders": [{"name": name, "path": str(path / name)} for name in folders[:MAX_FOLDERS]],
        "truncated": len(folders) > MAX_FOLDERS,
        "shortcuts": shortcuts(),
    }


# --------------------------------------------------------------------------
# Jobs: one command (or a chain of commands) running in the background
# --------------------------------------------------------------------------
class Step:
    def __init__(self, label: str, argv: list[str], stop_on: Callable[[int], bool] | None = None):
        self.label = label
        self.argv = argv
        self.stop_on = stop_on or (lambda rc: rc >= 2)   # 1 = some photos had problems: the next step still makes sense
        self.state = "waiting"                            # waiting, running, ok, problems, failed, stopped, skipped
        self.rc: int | None = None


class Job:
    def __init__(self, job_id: int, kind: str, title: str, steps: list[Step], params: dict, apply: bool):
        self.id = job_id
        self.kind = kind
        self.title = title
        self.steps = steps
        self.params = params
        self.apply = apply
        self.state = "running"          # running, finished, problems, failed, stopped
        self.rc: int | None = None
        self.lines: deque[str] = deque(maxlen=MAX_LINES)
        self.first = 0                  # number of the oldest line still kept
        self.count = 0                  # lines written so far
        self.started = time.time()
        self.ended: float | None = None
        self.stop_requested = False
        self.process: subprocess.Popen | None = None
        self.lock = threading.Lock()

    def write(self, line: str) -> None:
        with self.lock:
            if len(self.lines) == self.lines.maxlen:
                self.first += 1
            self.lines.append(line)
            self.count += 1

    def json(self, after: int = 0) -> dict:
        with self.lock:
            start = max(after, self.first)
            lines = list(self.lines)[start - self.first:] if start < self.count else []
            return {
                "id": self.id, "kind": self.kind, "title": self.title, "state": self.state, "rc": self.rc,
                "apply": self.apply, "params": self.params,
                "steps": [{"label": s.label, "state": s.state, "rc": s.rc} for s in self.steps],
                "lines": lines, "first": start, "next": self.count, "dropped": start > after,
                "started": self.started, "ended": self.ended,
            }


class JobRunner:
    """Runs one job at a time. `command` is how sort_photos.py is started (tests swap in a fake)."""

    def __init__(self, command: list[str] | None = None, on_finish: Callable[[Job], None] | None = None):
        self.command = command or [sys.executable, "-u", str(SCRIPT)]
        self.on_finish = on_finish
        self.job: Job | None = None
        self.next_id = 1
        self.lock = threading.Lock()

    def busy(self) -> bool:
        return self.job is not None and self.job.state == "running"

    def start(self, kind: str, title: str, steps: list[Step], params: dict, apply: bool) -> Job:
        with self.lock:
            if self.busy():
                raise HTTPException(409, f"'{self.job.title}' is still running. Wait for it to finish, or stop it.")
            job = Job(self.next_id, kind, title, steps, params, apply)
            self.next_id += 1
            self.job = job
        threading.Thread(target=self._run, args=(job,), name=f"job-{job.id}", daemon=True).start()
        return job

    def stop(self) -> Job | None:
        job = self.job
        if job is None or job.state != "running":
            return job
        job.stop_requested = True
        process = job.process
        if process and process.poll() is None:
            job.write("Stopping: the photo in progress is finished and everything done so far is kept ...")
            try:
                if os.name == "nt":
                    process.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    process.send_signal(signal.SIGINT)
            except (OSError, ValueError):
                process.terminate()
            timer = threading.Timer(STOP_GRACE, self._force_end, args=(job, process))
            timer.daemon = True
            timer.start()
        return job

    def shutdown(self) -> None:
        job = self.job
        if job and job.state == "running":
            job.stop_requested = True
            if job.process and job.process.poll() is None:
                job.process.terminate()

    @staticmethod
    def _force_end(job: Job, process: subprocess.Popen) -> None:
        if process.poll() is None:
            job.write("The command did not stop in time, so it was ended.")
            process.kill()

    def _spawn(self, argv: list[str]) -> subprocess.Popen:
        env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8", NO_COLOR="1")
        kwargs: dict = {}
        if os.name == "nt":  # its own process group (sharing this console), so a stop signal reaches only this command
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        return subprocess.Popen(
            [*self.command, *argv], cwd=str(APP_DIR), env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", bufsize=1, **kwargs,
        )

    def _run(self, job: Job) -> None:
        worst = 0
        try:
            for number, step in enumerate(job.steps, 1):
                if job.stop_requested:
                    step.state = "skipped"
                    continue
                step.state = "running"
                if len(job.steps) > 1:
                    job.write(f"=== Step {number} of {len(job.steps)}: {step.label} ===")
                job.write("$ sort_photos.py " + subprocess.list2cmdline(step.argv))
                try:
                    job.process = self._spawn(step.argv)
                except OSError as error:
                    job.write(f"Could not start it: {error}")
                    step.state, step.rc = "failed", 2
                    worst = 2
                    break
                for line in job.process.stdout:
                    job.write(line.rstrip("\r\n"))
                rc = job.process.wait()
                step.rc = rc
                if job.stop_requested and rc != 0:
                    step.state = "stopped"
                    worst = 2
                    continue
                step.state = "ok" if rc == 0 else "problems" if rc == 1 else "failed"
                worst = max(worst, rc if rc in (0, 1) else 2)
                if step.stop_on(rc):
                    for later in job.steps[number:]:
                        later.state = "skipped"
                    break
        except Exception as error:  # never leave the page waiting on a job that died
            job.write(f"Unexpected problem: {error}")
            worst = 2
        finally:
            job.process = None
            job.rc = worst
            job.state = ("stopped" if job.stop_requested and worst else
                         "finished" if worst == 0 else "problems" if worst == 1 else "failed")
            job.ended = time.time()
            job.write({"finished": "Finished.", "problems": "Finished, with some problems (see above).",
                       "failed": "Did not finish (see above).", "stopped": "Stopped. Run it again to continue where it left off."}[job.state])
            if self.on_finish:
                try:
                    self.on_finish(job)
                except Exception:
                    pass


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------
class SortBody(BaseModel):
    source: str
    library: str
    move: bool = False
    apply: bool = False


class FacesBody(BaseModel):
    library: str
    limit: int | None = Field(default=None, ge=1)
    hardware: str = "auto"


class SearchBody(BaseModel):
    model: str
    apply: bool = False
    download: bool = False


class LibraryRootBody(BaseModel):
    path: str


class LibraryTaskBody(BaseModel):
    apply: bool = True
    faces: bool = True
    search: bool = True
    model: str = ""
    download: bool = False
    hardware: str = "auto"


def library_status(db_path: Path, root: str, model: str) -> dict:
    """How much of the library at `root` is indexed, scanned for faces and searchable (photos elsewhere are counted apart)."""
    import sqlite3
    import sort_photos as sp
    status = {"root": root, "exists": bool(root) and Path(root).is_dir(), "photos": 0, "outside": 0,
              "scanned": 0, "searchable": 0, "model": model}
    if not db_path.exists():
        return status
    prefix = sp._norm(root) + os.sep if root else None
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        scanned = "EXISTS (SELECT 1 FROM face_scans s WHERE s.photo_id = p.id AND s.error IS NULL)" if "face_scans" in tables else "0"
        vectors = ("EXISTS (SELECT 1 FROM photo_embeddings e WHERE e.photo_id = p.id AND e.model = ?)"
                   if "photo_embeddings" in tables and model else "0")
        params = [model] if vectors != "0" else []
        for path, has_scan, has_vector in conn.execute(f"SELECT p.sorted_path, {scanned}, {vectors} FROM photos p", params):
            if prefix and sp._norm(path).startswith(prefix):
                status["photos"] += 1
                status["scanned"] += bool(has_scan)
                status["searchable"] += bool(has_vector)
            else:
                status["outside"] += 1
    except sqlite3.Error:
        pass
    finally:
        conn.close()
    return status


def package_problem(*modules: str) -> str | None:
    import importlib.util
    missing = [m for m in modules if importlib.util.find_spec(m) is None]
    return " ".join(missing) or None


def add_task_routes(app: FastAPI, db_path: Path, runner: JobRunner, model_root: Path | None = None,
                    regroup_busy: Callable[[], bool] = lambda: False) -> None:
    db_args = ["--db", str(db_path)]
    root_args = ["--model-root", str(model_root)] if model_root else []

    def start(kind: str, title: str, steps: list[Step], params: dict, apply: bool) -> dict:
        if regroup_busy():
            raise HTTPException(409, "Faces are being placed right now. Try again when that has finished.")
        return runner.start(kind, title, steps, params, apply).json()

    def search_models() -> tuple[list[dict], str]:
        try:
            import clip_search
        except ImportError:
            return [], ""
        default = conf_get("SEARCH_MODEL") or clip_search.DEFAULT_MODEL
        names = list(clip_search.SUGGESTED_MODELS)
        folder_root = model_root or clip_search.default_model_root()
        try:
            names += sorted(p.name for p in Path(folder_root).iterdir() if p.is_dir() and p.name not in names)
        except OSError:
            pass
        if default not in names:
            names.append(default)
        models = []
        for name in names:
            try:
                downloaded = not clip_search.missing_files(clip_search.model_folder(model_root, name))
            except clip_search.ModelError:
                continue
            size, about = clip_search.SUGGESTED_MODELS.get(name, (clip_search.size_hint(name), ""))
            models.append({"name": name, "size": size, "about": about, "downloaded": downloaded})
        return models, default

    @app.get("/api/tasks")
    def task_info():
        models, default = search_models()
        faces = package_problem("insightface", "onnxruntime")
        search = package_problem("tokenizers", "onnxruntime")
        return {
            "folders": {"source": conf_get("LAST_SOURCE"), "library": conf_get("LIBRARY_DIR")},
            "packages": {
                "faces": f"Face scanning needs Python packages that are not installed ({faces}). "
                         "Install them with: pip install -r requirements-faces.txt" if faces else None,
                "search": f"Search needs Python packages that are not installed ({search}). "
                          "Install them with: pip install -r requirements-search.txt" if search else None,
            },
            "search": {"models": models, "default": default},
            "library": library_status(db_path, conf_get("LIBRARY_DIR"), default),
            "job": runner.job.json() if runner.job else None,
        }

    @app.get("/api/folders")
    def folders(path: str = ""):
        return list_folders(path)

    @app.get("/api/jobs/current")
    def current_job(after: int = 0):
        return {"job": runner.job.json(max(0, after)) if runner.job else None}

    @app.post("/api/jobs/current/stop")
    def stop_job():
        job = runner.stop()
        return {"job": job.json() if job else None}

    @app.post("/api/tasks/sort")
    def sort_task(body: SortBody):
        source = absolute_folder(body.source, "folder with the new photos", True)
        library = absolute_folder(body.library, "library folder", False)
        if source.resolve() == library.resolve():
            raise HTTPException(400, "The new photos and the library must be different folders.")
        conf_set("LAST_SOURCE", str(source))
        conf_set("LIBRARY_DIR", str(library))
        argv = ["sort", str(source), str(library), *(["--move"] if body.move else []), *db_args]
        verb = "Move" if body.move else "Copy"
        if body.apply:
            title = f"Sorting new photos ({verb.lower()})"
            argv.append("--apply")
        else:
            title = f"Preview: sort new photos ({verb.lower()})"
        params = {"source": str(source), "library": str(library), "move": body.move}
        return start("sort", title, [Step("Sort", argv)], params, body.apply)

    @app.post("/api/tasks/faces")
    def faces_task(body: FacesBody):
        library = absolute_folder(body.library, "library folder", True)
        conf_set("LIBRARY_DIR", str(library))
        hardware = {"gpu": ["--gpu"], "cpu": ["--cpu"]}.get(body.hardware, [])
        limit = ["--limit", str(body.limit)] if body.limit else []
        steps = [
            Step("Refresh the index", ["refresh", str(library), "--apply", *db_args]),
            Step("Find faces", ["faces", "--apply", *limit, *hardware, *db_args]),
            Step("Group faces into people", ["cluster", "--apply", *db_args]),
        ]
        params = {"library": str(library), "limit": body.limit, "hardware": body.hardware}
        return start("faces", "Finding faces and grouping people", steps, params, True)

    @app.post("/api/tasks/group")
    def group_task():
        return start("group", "Grouping faces into people", [Step("Group faces into people", ["cluster", "--apply", *db_args])], {}, True)

    def search_steps(model: str, download: bool) -> tuple[list[Step], bool]:
        """The steps that store search vectors with `model`, downloading it first when it is missing and that was agreed."""
        try:
            import clip_search
        except ImportError as error:
            raise HTTPException(409, f"Search needs extra packages ({error}). Install them with: pip install -r requirements-search.txt")
        try:
            downloaded = not clip_search.missing_files(clip_search.model_folder(model_root, model))
        except clip_search.ModelError as error:
            raise HTTPException(400, str(error))
        steps = []
        if not downloaded:
            if not download:
                raise HTTPException(409, f"The search model {model} is not downloaded yet (about {clip_search.size_hint(model)}).")
            steps.append(Step("Download and check the model",
                              ["clipcheck", "--model", model, "--download", "--no-cpu-compare", *root_args, *db_args],
                              stop_on=lambda rc: rc != 0))
        steps.append(Step("Store search vectors", ["embed", "--apply", "--model", model, *root_args, *db_args]))
        return steps, downloaded

    @app.post("/api/tasks/search")
    def search_task(body: SearchBody):
        model = body.model.strip()
        if not body.apply:
            try:
                import clip_search
                downloaded = not clip_search.missing_files(clip_search.model_folder(model_root, model))
            except ImportError as error:
                raise HTTPException(409, f"Search needs extra packages ({error}). Install them with: pip install -r requirements-search.txt")
            except clip_search.ModelError as error:
                raise HTTPException(400, str(error))
            steps = [Step("Preview", ["embed", "--model", model, *root_args, *db_args])]
            return start("search", "Preview: prepare search", steps, {"model": model, "downloaded": downloaded}, False)
        steps, downloaded = search_steps(model, body.download)
        conf_set("SEARCH_MODEL", model)
        return start("search", "Preparing search", steps, {"model": model, "downloaded": downloaded}, True)

    # ------------------------------------------------------------ the photo library (its root folder)
    def default_search_model() -> str:
        try:
            import clip_search
        except ImportError:
            return conf_get("SEARCH_MODEL")
        return conf_get("SEARCH_MODEL") or clip_search.DEFAULT_MODEL

    @app.get("/api/library/root")
    def library_root():
        return library_status(db_path, conf_get("LIBRARY_DIR"), default_search_model())

    @app.post("/api/library/root")
    def set_library_root(body: LibraryRootBody):
        """Change the photo library folder. Nothing is indexed or removed until the library task runs."""
        if runner.busy():
            raise HTTPException(409, f"'{runner.job.title}' is still running. Change the library when it has finished.")
        root = absolute_folder(body.path, "library folder", False)
        conf_set("LIBRARY_DIR", str(root))
        return library_status(db_path, str(root), default_search_model())

    @app.post("/api/tasks/library")
    def library_task(body: LibraryTaskBody):
        """Bring the index in line with the library: add (and re-point) photos, then find faces, group, store search vectors."""
        root = absolute_folder(conf_get("LIBRARY_DIR"), "library folder", True)
        if not body.apply:
            steps = [Step("Preview", ["refresh", str(root), "--adopt", *db_args])]
            return start("library", "Preview: add this library", steps, {"library": str(root)}, False)
        steps = [Step("Add the photos to the index", ["refresh", str(root), "--apply", "--adopt", *db_args])]
        if body.faces:
            hardware = {"gpu": ["--gpu"], "cpu": ["--cpu"]}.get(body.hardware, [])
            # faces and search don't depend on each other: a problem with one doesn't stop the other
            steps += [Step("Find faces", ["faces", "--apply", *hardware, *db_args], stop_on=lambda rc: False),
                      Step("Group faces into people", ["cluster", "--apply", *db_args], stop_on=lambda rc: False)]
        model = ""
        if body.search:
            model = body.model.strip() or default_search_model()
            steps += search_steps(model, body.download)[0]
            conf_set("SEARCH_MODEL", model)
        params = {"library": str(root), "faces": body.faces, "search": body.search, "model": model}
        return start("library", "Adding the library", steps, params, True)
