"""Phase 2: find the faces in every indexed photo and store a face "embedding".

The AI runs completely offline once the model has been downloaded. It does NOT
name anyone: an embedding is a list of 512 numbers that is very similar for
photos of the same person. Phase 3 groups similar embeddings into people, and
phase 4 is the web UI where you put names on the groups.

This module is used through sort_photos.py:

    python sort_photos.py faces                      # preview
    python sort_photos.py faces --apply --limit 20   # try it on 20 photos first
    python sort_photos.py faces --apply              # everything not yet scanned

Scanning is resumable: photos already scanned are skipped and every photo is
saved as soon as it is done, so you can stop with Ctrl+C and continue later.

For each face the index stores: its position in the photo, detection score,
face size in pixels, a sharpness number, head turn (yaw), the embedding and a
small thumbnail (so the naming UI never needs to re-read your original photos).

Model: InsightFace "buffalo_l" (face detector + ArcFace recognizer). Its
pretrained models are licensed for non-commercial/research use, which covers a
personal photo library.
"""
from __future__ import annotations

import importlib.metadata as importlib_metadata
import io
import math
import os
import platform
import shutil
import sqlite3
import subprocess
import time
import warnings
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageOps

import sort_photos as sp

log = sp.log

THUMB_SIZE = 128  # pixels, square
THUMB_MARGIN = 0.4  # extra context around the face box, as a fraction of its size
BLUR_PATCH = 112  # faces are resized to this before measuring sharpness
PROGRESS_EVERY = 25
MAX_CONSECUTIVE_DETECT_FAILURES = 5
GPU_PROVIDER = "CUDAExecutionProvider"  # NVIDIA
GPU_SETUP_STEPS = (
    "To use your NVIDIA GPU, swap in the GPU build of ONNX Runtime in this same virtual environment:\n"
    "    pip uninstall -y onnxruntime onnxruntime-gpu\n"
    '    pip install "onnxruntime-gpu[cuda,cudnn]"\n'
    "(InsightFace installs the CPU build, and the two builds cannot be installed together.)\n"
    "Then check it with: python sort_photos.py gpucheck"
)
PROVIDER_LABELS = {
    "CUDAExecutionProvider": "NVIDIA GPU (CUDA)",
    "DmlExecutionProvider": "GPU (DirectML)",
    "CPUExecutionProvider": "CPU",
}


# --------------------------------------------------------------------------
# Runtime / model
# --------------------------------------------------------------------------
def runtime_problem() -> str | None:
    """Return a description of what is missing, or None if face scanning can run."""
    try:
        import insightface  # noqa: F401
        import onnxruntime  # noqa: F401
    except Exception as error:  # ImportError, or a broken native install
        return f"{type(error).__name__}: {error}"
    return None


def hardware_mode(args) -> str:
    """'cpu' (--cpu), 'gpu' (--gpu: NVIDIA required) or 'auto' (use a GPU if one is set up)."""
    if getattr(args, "cpu", False):
        return "cpu"
    return "gpu" if getattr(args, "gpu", False) else "auto"


def pick_providers(mode: str = "auto") -> tuple[list[str], bool]:
    """Choose the ONNX Runtime providers. Returns (providers, uses_gpu).

    Raises RuntimeError in 'gpu' mode if this ONNX Runtime has no NVIDIA CUDA support.
    """
    import onnxruntime as ort

    available = ort.get_available_providers()
    if mode == "cpu":
        return ["CPUExecutionProvider"], False
    if GPU_PROVIDER in available:
        return [GPU_PROVIDER, "CPUExecutionProvider"], True
    if mode == "gpu":
        raise RuntimeError(
            "--gpu was given, but the installed ONNX Runtime has no NVIDIA CUDA support "
            f"(it offers: {', '.join(available)}).\n{GPU_SETUP_STEPS}"
        )
    if "DmlExecutionProvider" in available:  # DirectML build: any DirectX 12 GPU
        return ["DmlExecutionProvider", "CPUExecutionProvider"], True
    return ["CPUExecutionProvider"], False


def preload_cuda_libraries() -> str | None:
    """Ask ONNX Runtime to load the CUDA/cuDNN libraries (including ones pip installed). Returns a problem or None."""
    import onnxruntime as ort

    preload = getattr(ort, "preload_dlls", None)
    if preload is None:
        return None
    try:
        preload()
    except Exception as error:
        return f"{type(error).__name__}: {error}"
    return None


def installed_onnxruntime_packages() -> dict[str, str]:
    found = {}
    for name in ("onnxruntime", "onnxruntime-gpu", "onnxruntime-directml"):
        try:
            found[name] = importlib_metadata.version(name)
        except importlib_metadata.PackageNotFoundError:
            pass
    return found


def nvidia_gpu_summary() -> str | None:
    """GPU name, driver and memory from nvidia-smi, or None if it is not available."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    try:
        result = subprocess.run(
            [exe, "--query-gpu=name,driver_version,memory.total", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return "; ".join(lines) if result.returncode == 0 and lines else None


def model_root_path(model_root: Path | None) -> Path:
    return Path(os.path.expanduser(str(model_root) if model_root else "~/.insightface"))


class FaceEngine:
    """Thin wrapper around InsightFace so the rest of the code stays simple.

    After loading, `provider` is the hardware the models REALLY run on. ONNX Runtime quietly
    falls back to the CPU when it cannot start the GPU, so this is checked, not assumed.
    """

    def __init__(self, model: str, model_root: Path | None, det_size: int, min_score: float, mode: str = "auto"):
        # InsightFace emits harmless deprecation warnings from its own code.
        warnings.filterwarnings("ignore", category=FutureWarning, module=r"insightface.*")
        from insightface.app import FaceAnalysis

        providers, wants_gpu = pick_providers(mode)  # may raise in 'gpu' mode
        self.model = model
        self.requested = providers[0]
        if self.requested == GPU_PROVIDER:
            problem = preload_cuda_libraries()
            if problem:
                log.warning("Could not preload the CUDA libraries: %s", problem)
        self.app = FaceAnalysis(
            name=model,
            root=str(model_root_path(model_root)),
            # No age/gender module: not needed and not wanted.
            allowed_modules=["detection", "recognition", "landmark_3d_68"],
            providers=providers,
        )
        # ctx_id < 0 forces the CPU inside InsightFace, so use 0 whenever a GPU provider is chosen.
        self.app.prepare(ctx_id=0 if wants_gpu else -1, det_thresh=min_score, det_size=(det_size, det_size))

        self.session_providers = {
            name: m.session.get_providers() for name, m in self.app.models.items() if hasattr(m, "session")
        }
        used = {p[0] for p in self.session_providers.values() if p}
        self.provider = used.pop() if len(used) == 1 else "mixed"
        for name, active in self.session_providers.items():
            log.debug("Model part %s runs on %s", name, active[0] if active else "?")
        if wants_gpu and self.provider != self.requested:
            detail = ", ".join(f"{n}: {p[0] if p else '?'}" for n, p in self.session_providers.items())
            if mode == "gpu":
                raise RuntimeError(
                    f"The NVIDIA GPU could not be started, so the models would have run on the CPU ({detail}). "
                    "See the ONNX Runtime message above for the reason, and run 'python sort_photos.py gpucheck' for a diagnosis."
                )
            log.warning(
                "%s was available but could not be started, so this runs on the CPU instead (%s). "
                "Run 'python sort_photos.py gpucheck' to find out why.",
                PROVIDER_LABELS.get(self.requested, self.requested), detail,
            )

    def detect(self, bgr: np.ndarray) -> list[dict]:
        found = []
        for face in self.app.get(bgr):
            x1, y1, x2, y2 = (float(v) for v in face.bbox)
            pose = getattr(face, "pose", None)  # (pitch, yaw, roll) in degrees
            found.append(
                {
                    "bbox": (x1, y1, x2, y2),
                    "score": float(face.det_score),
                    "embedding": face.normed_embedding,
                    "yaw": float(pose[1]) if pose is not None else None,
                }
            )
        return found


# --------------------------------------------------------------------------
# Images
# --------------------------------------------------------------------------
def load_image(path: str, max_side: int) -> tuple[Image.Image, tuple[int, int]]:
    """Open a photo upright (EXIF rotation applied), as RGB, shrunk to max_side.

    Returns (image, (original_width, original_height)) where the size is that of
    the upright photo before shrinking.
    """
    with Image.open(path) as opened:
        img = ImageOps.exif_transpose(opened).convert("RGB")
    orig_w, orig_h = img.size
    longest = max(orig_w, orig_h)
    if longest > max_side:
        scale = max_side / longest
        img = img.resize((max(1, round(orig_w * scale)), max(1, round(orig_h * scale))), Image.LANCZOS)
    return img, (orig_w, orig_h)


def iter_loaded(items: list[tuple[int, str]], max_side: int, workers: int, lookahead: int = 6):
    """Yield (item, loaded, error) while background threads read the next photos.

    Reading photos (often from a network drive) is the slow part, so this keeps
    the face model busy instead of waiting on the disk.
    """
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        queue: deque = deque()
        source = iter(items)

        def submit() -> bool:
            try:
                item = next(source)
            except StopIteration:
                return False
            queue.append((item, pool.submit(load_image, item[1], max_side)))
            return True

        for _ in range(max(lookahead, workers)):
            if not submit():
                break
        while queue:
            item, future = queue.popleft()
            submit()
            try:
                yield item, future.result(), None
            except Exception as error:  # unreadable, corrupt, unsupported, missing...
                yield item, None, error


def make_thumb(img: Image.Image, box: tuple[float, float, float, float]) -> bytes:
    x1, y1, x2, y2 = box
    cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
    half = max(x2 - x1, y2 - y1) * (1 + 2 * THUMB_MARGIN) / 2
    crop = img.crop((round(cx - half), round(cy - half), round(cx + half), round(cy + half)))
    crop = crop.resize((THUMB_SIZE, THUMB_SIZE), Image.LANCZOS)
    buffer = io.BytesIO()
    crop.save(buffer, "JPEG", quality=85)
    return buffer.getvalue()


def analyse_image(engine: FaceEngine, img: Image.Image, orig_size: tuple[int, int]) -> list[dict]:
    """Detect faces in one (already upright) image and build the rows to store."""
    small_w, small_h = img.size
    orig_w, _ = orig_size
    rgb = np.asarray(img)
    bgr = np.ascontiguousarray(rgb[:, :, ::-1])
    rows = []
    for det in engine.detect(bgr):
        x1, y1, x2, y2 = det["bbox"]
        cx1, cy1 = max(0.0, x1), max(0.0, y1)
        cx2, cy2 = min(float(small_w), x2), min(float(small_h), y2)
        if cx2 - cx1 < 2 or cy2 - cy1 < 2:
            continue  # box is (almost) entirely outside the photo
        patch = rgb[int(cy1):math.ceil(cy2), int(cx1):math.ceil(cx2)]
        gray = cv2.cvtColor(cv2.resize(patch, (BLUR_PATCH, BLUR_PATCH), interpolation=cv2.INTER_AREA), cv2.COLOR_RGB2GRAY)
        rows.append(
            {
                "x1": cx1 / small_w, "y1": cy1 / small_h, "x2": cx2 / small_w, "y2": cy2 / small_h,
                "det_score": det["score"],
                "face_px": int(round(min(x2 - x1, y2 - y1) * orig_w / small_w)),  # in the original photo
                "blur": float(cv2.Laplacian(gray, cv2.CV_64F).var()),  # higher = sharper
                "yaw": det["yaw"],
                "embedding": np.ascontiguousarray(det["embedding"], dtype=np.float32).tobytes(),
                "thumb": make_thumb(img, (x1, y1, x2, y2)),
            }
        )
    return rows


# --------------------------------------------------------------------------
# Index
# --------------------------------------------------------------------------
def has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def open_read_only(db_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)


def pending_photos(conn: sqlite3.Connection, model: str, rescan: bool, retry_errors: bool, has_scans: bool):
    """Photos that still need scanning, oldest index entry first."""
    if rescan or not has_scans:
        return conn.execute("SELECT id, sorted_path FROM photos ORDER BY id").fetchall()
    conditions = ["fs.photo_id IS NULL", "(fs.error IS NULL AND fs.model != ?)"]  # never scanned / scanned by another model
    params: list = [model]
    if retry_errors:
        conditions.append("fs.error IS NOT NULL")
    sql = (
        "SELECT p.id, p.sorted_path FROM photos p LEFT JOIN face_scans fs ON fs.photo_id = p.id"
        f" WHERE {' OR '.join(conditions)} ORDER BY p.id"
    )
    return conn.execute(sql, params).fetchall()


def save_result(conn: sqlite3.Connection, photo_id: int, model: str, rows: list[dict], error: str | None) -> None:
    """Replace everything stored for one photo, in a single transaction."""
    with conn:
        conn.execute("DELETE FROM faces WHERE photo_id = ?", (photo_id,))
        for r in rows:
            conn.execute(
                "INSERT INTO faces (photo_id, x1, y1, x2, y2, det_score, face_px, blur, yaw, embedding, thumb, model)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (photo_id, r["x1"], r["y1"], r["x2"], r["y2"], r["det_score"], r["face_px"],
                 r["blur"], r["yaw"], r["embedding"], r["thumb"], model),
            )
        conn.execute(
            "INSERT OR REPLACE INTO face_scans (photo_id, scanned_at, model, face_count, error) VALUES (?, ?, ?, ?, ?)",
            (photo_id, datetime.now().isoformat(timespec="seconds"), model, len(rows), error),
        )


def fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


# --------------------------------------------------------------------------
# The faces command
# --------------------------------------------------------------------------
def scan_faces(args) -> int:
    started = time.monotonic()
    db_path = (args.db or sp.default_db_path()).resolve()
    log.info("=== Face scan started (%s) ===", "apply" if args.apply else "dry run")
    log.info(
        "Model: %s | detection size: %d | min score: %.2f | photos shrunk to %d px",
        args.model, args.det_size, args.min_score, args.max_side,
    )
    sp.log_index_location(db_path, args.apply)
    if not db_path.exists():
        log.error("There is no index yet. Run 'sort ... --apply' or 'refresh ... --apply' first, so there are photos to scan.")
        return 2

    try:
        conn = sp.open_index(db_path) if args.apply else open_read_only(db_path)
    except (OSError, sqlite3.Error) as error:
        log.error("Cannot open the index %s: %s", db_path, error)
        return 2

    has_scans = has_table(conn, "face_scans")
    total = conn.execute("SELECT COUNT(*) FROM photos").fetchone()[0]
    scanned = failed = 0
    if has_scans:
        scanned, failed = conn.execute(
            "SELECT COALESCE(SUM(error IS NULL), 0), COALESCE(SUM(error IS NOT NULL), 0) FROM face_scans"
        ).fetchone()
    pending = pending_photos(conn, args.model, args.rescan, args.retry_errors, has_scans)
    todo = pending[: args.limit] if args.limit else pending
    log.info("Photos in the index: %d | scanned before: %d | earlier errors: %d", total, scanned, failed)
    if failed and not args.retry_errors and not args.rescan:
        log.info("  (photos that failed before are skipped; add --retry-errors to try them again)")
    limited = f" (limited from {len(pending)} by --limit)" if len(todo) < len(pending) else ""
    log.info("To scan now: %d%s", len(todo), limited)

    # Re-scanning a photo replaces its face rows, which would also erase the names you
    # confirmed for those faces. Refuse unless the person explicitly allows it.
    if todo and has_table(conn, "face_person"):
        protected = {
            row[0] for row in conn.execute(
                "SELECT DISTINCT f.photo_id FROM face_person fp JOIN faces f ON f.id = fp.face_id WHERE fp.status = 'confirmed'"
            )
        }
        hit = sum(1 for photo_id, _ in todo if photo_id in protected)
        if hit:
            message = (
                "%d of these photos hold faces you confirmed in the UI. Scanning them again would erase those "
                "confirmations. Add --discard-confirmed to scan them anyway, or use --limit / skip --rescan."
            )
            if not args.discard_confirmed:
                if args.apply:
                    log.error(message, hit)
                    return 2
                log.warning(message, hit)
            else:
                log.warning("%d photo(s) hold confirmed faces; their confirmations will be discarded (--discard-confirmed).", hit)

    problem = runtime_problem()
    if problem:
        message = (
            "Face scanning needs InsightFace and ONNX Runtime (%s). "
            "Install them with: pip install -r requirements-faces.txt"
        )
        if args.apply:
            log.error(message, problem)
            conn.close()
            return 2
        log.warning(message, problem)
    else:
        mode = hardware_mode(args)
        try:
            providers, uses_gpu = pick_providers(mode)
        except RuntimeError as error:
            log.error("%s", error)
            conn.close()
            return 2
        log.info("Hardware that will be used: %s", PROVIDER_LABELS.get(providers[0], providers[0]))
        if not uses_gpu and mode == "auto" and shutil.which("nvidia-smi"):
            log.info(
                "Tip: an NVIDIA GPU seems to be installed, but ONNX Runtime is not set up for it, so this runs on the CPU. "
                "Run 'python sort_photos.py gpucheck' to see how to enable it."
            )

    if not todo:
        log.info("Nothing to scan.")
        conn.close()
        return 0
    if not args.apply:
        log.info("Roughly 0.3 to 1 second per photo on a CPU, plus time to read the photos (slower over a network).")
        log.info("Dry run only: nothing was scanned or written. Re-run with --apply to scan (try --limit 20 first).")
        conn.close()
        return 0

    root = model_root_path(args.model_root)
    if not (root / "models" / args.model).exists():
        log.info(
            "First run: downloading the %s model (about 280 MB) into %s. After that, everything runs offline.",
            args.model, root,
        )
    log.info("Loading the face model ...")
    try:
        engine = FaceEngine(args.model, args.model_root, args.det_size, args.min_score, hardware_mode(args))
    except Exception as error:
        log.error("Could not load the face model: %s: %s", type(error).__name__, error)
        conn.close()
        return 2
    log.info("Model ready, running on %s (%.1f s to load)", PROVIDER_LABELS.get(engine.provider, engine.provider), time.monotonic() - started)

    stats: Counter[str] = Counter()
    consecutive_detect_failures = 0
    aborted: str | None = None
    interrupted = False
    loop_started = time.monotonic()
    log.info("Scanning %d photos (Ctrl+C stops safely; progress is saved after every photo) ...", len(todo))
    try:
        for (photo_id, path), loaded, load_error in iter_loaded(list(todo), args.max_side, args.workers):
            rows: list[dict] = []
            error: str | None = None
            if isinstance(load_error, FileNotFoundError):
                # Deleted (or its drive is offline). Not an error to record: 'refresh' removes it from the index.
                stats["missing"] += 1
                log.warning("File no longer exists, skipped: %s", path)
                continue
            if load_error is not None:
                error = f"{type(load_error).__name__}: {load_error}"[:300]
                log.warning("Could not read %s: %s", path, error)
            else:
                img, orig_size = loaded
                try:
                    rows = analyse_image(engine, img, orig_size)
                    consecutive_detect_failures = 0
                except Exception as exc:
                    consecutive_detect_failures += 1
                    error = f"face detection failed: {type(exc).__name__}: {exc}"[:300]
                    log.warning("%s: %s", path, error)
            try:
                save_result(conn, photo_id, engine.model, rows, error)
            except sqlite3.Error as exc:
                log.error("Could not write to the index: %s. Stopping so no work is lost silently.", exc)
                aborted = "index write failed"
                break
            stats["processed"] += 1  # counted only once its result is saved

            if error:
                stats["errors"] += 1
            elif rows:
                stats["with_faces"] += 1
                stats["faces"] += len(rows)
                log.debug(
                    "%s: %d face(s) [%s]", path, len(rows),
                    ", ".join(f"score {r['det_score']:.2f} {r['face_px']}px" for r in rows),
                )
            else:
                stats["no_faces"] += 1
                log.debug("%s: no faces", path)

            if stats["processed"] % PROGRESS_EVERY == 0:
                elapsed = time.monotonic() - loop_started
                rate = stats["processed"] / elapsed if elapsed else 0.0
                left = (len(todo) - stats["processed"]) / rate if rate else 0.0
                log.info(
                    "Progress: %d/%d photos, %d faces found, %.1f photos/s, about %s left",
                    stats["processed"], len(todo), stats["faces"], rate, fmt_duration(left),
                )
            if consecutive_detect_failures >= MAX_CONSECUTIVE_DETECT_FAILURES:
                aborted = (
                    f"face detection failed on {consecutive_detect_failures} photos in a row, which points to a "
                    "problem with the model or GPU (try --cpu). Those photos were recorded as errors; "
                    "re-run with --retry-errors once it is fixed"
                )
                break
    except KeyboardInterrupt:
        interrupted = True
        log.warning("Stopped by you. Everything scanned so far is saved; run the same command again to continue.")

    elapsed = time.monotonic() - loop_started
    face_total = conn.execute("SELECT COUNT(*) FROM faces").fetchone()[0]
    scanned_total = conn.execute("SELECT COUNT(*) FROM face_scans WHERE error IS NULL").fetchone()[0]
    conn.close()

    log.info("--- Face scan summary ---")
    log.info("%-22s%d of %d", "Photos processed:", stats["processed"], len(todo))
    log.info("%-22s%d", "  with faces:", stats["with_faces"])
    log.info("%-22s%d", "  without faces:", stats["no_faces"])
    log.info("%-22s%d", "  errors:", stats["errors"])
    average = stats["faces"] / stats["with_faces"] if stats["with_faces"] else 0.0
    log.info("%-22s%d (%.1f per photo that has faces)", "Faces found:", stats["faces"], average)
    if stats["processed"] and elapsed:
        log.info("%-22s%.1f photos/s", "Speed:", stats["processed"] / elapsed)
    log.info("%-22s%d faces from %d photos", "Index now holds:", face_total, scanned_total)
    if stats["missing"]:
        log.warning(
            "%d photos no longer exist on disk and were skipped (is the drive connected?). If they were deleted, "
            "run 'sort_photos.py refresh <library> --apply' to remove them from the index.",
            stats["missing"],
        )
    if aborted:
        log.error("Stopped early: %s", aborted)
    sp.log_index_location(db_path, True)
    log.info("Finished in %s", fmt_duration(time.monotonic() - started))
    if not aborted and not interrupted and len(todo) < len(pending):
        log.info("%d more photos still to scan; run the command again to continue.", len(pending) - len(todo))
    if not aborted and not interrupted and len(todo) == len(pending):
        log.info("Next step (phase 3): group these faces into people.")
    if interrupted:
        return 130  # the conventional exit code for Ctrl+C
    return 1 if aborted or stats["errors"] else 0


# --------------------------------------------------------------------------
# The gpucheck command
# --------------------------------------------------------------------------
def _time_detection(engine: FaceEngine, image: np.ndarray, runs: int) -> tuple[int, float]:
    """Return (faces found, average seconds per photo) after one warm-up run."""
    engine.detect(image)  # the first call is slow while the GPU sets itself up
    found = 0
    started = time.perf_counter()
    for _ in range(max(1, runs)):
        found = len(engine.detect(image))
    return found, (time.perf_counter() - started) / max(1, runs)


def gpu_check(args) -> int:
    """Explain whether the NVIDIA GPU is usable, and prove it with a timed test."""
    log.info("=== GPU check ===")
    log.info("Python %s on %s", platform.python_version(), platform.platform())
    smi = nvidia_gpu_summary()
    if smi:
        log.info("NVIDIA GPU (name; driver; memory): %s", smi)
    else:
        log.warning(
            "nvidia-smi was not found or did not answer. Is an NVIDIA graphics driver installed? "
            "(Download the current driver from nvidia.com.)"
        )

    problem = runtime_problem()
    if problem:
        log.error("InsightFace / ONNX Runtime cannot be loaded (%s). Run: pip install -r requirements-faces.txt", problem)
        return 1
    import onnxruntime as ort

    packages = installed_onnxruntime_packages()
    log.info(
        "ONNX Runtime %s; installed packages: %s",
        ort.__version__, ", ".join(f"{n} {v}" for n, v in packages.items()) or "unknown",
    )
    if "onnxruntime" in packages and "onnxruntime-gpu" in packages:
        log.warning(
            "Both 'onnxruntime' (CPU) and 'onnxruntime-gpu' are installed. They overwrite each other's files, "
            "which often breaks the GPU. Fix:\n%s", GPU_SETUP_STEPS,
        )
    available = ort.get_available_providers()
    log.info("Execution providers available: %s", ", ".join(available))
    if GPU_PROVIDER not in available:
        log.error("NVIDIA CUDA is NOT available to ONNX Runtime: the installed build is CPU-only.\n%s", GPU_SETUP_STEPS)
        return 1

    log.info("Loading the face model on the GPU (this also loads the CUDA/cuDNN libraries) ...")
    try:
        gpu_engine = FaceEngine(args.model, args.model_root, args.det_size, 0.5, "gpu")
    except Exception as error:
        log.error("The GPU could not be used: %s", error)
        log.error(
            "Common causes: an out-of-date NVIDIA driver; the CUDA/cuDNN libraries missing (install the GPU build "
            'with the extras: pip install "onnxruntime-gpu[cuda,cudnn]"); or a GPU too old for this build. '
            "The current onnxruntime-gpu targets CUDA 13, which needs a recent driver and a GTX 16-series/RTX 20-series "
            'or newer GPU. For an older GPU try the CUDA 12 build: pip install "onnxruntime-gpu[cuda,cudnn]==1.26.0".'
        )
        return 1
    log.info("Every model part is running on: %s", PROVIDER_LABELS.get(gpu_engine.provider, gpu_engine.provider))

    from insightface.data import get_image  # a sample photo that ships with InsightFace

    image = get_image("t1")
    log.info("Timing face detection on a sample photo (%d runs) ...", args.runs)
    faces, gpu_seconds = _time_detection(gpu_engine, image, args.runs)
    log.info("GPU: %.0f ms per photo (%d faces found in the sample)", gpu_seconds * 1000, faces)
    del gpu_engine

    log.info("Timing the same on the CPU for comparison ...")
    cpu_engine = FaceEngine(args.model, args.model_root, args.det_size, 0.5, "cpu")
    _, cpu_seconds = _time_detection(cpu_engine, image, args.runs)
    log.info("CPU: %.0f ms per photo", cpu_seconds * 1000)

    speedup = cpu_seconds / gpu_seconds if gpu_seconds else 0.0
    if speedup >= 1.2:
        log.info("RESULT: your NVIDIA GPU is working, about %.1fx faster than the CPU on this test.", speedup)
    else:
        log.warning(
            "RESULT: the GPU runs, but it was only %.1fx the CPU speed here. That can happen with one small test photo; "
            "judge it on a real scan (python sort_photos.py faces --apply --limit 50).", speedup,
        )
    log.info("Use it: python sort_photos.py faces --apply   (add --gpu to refuse to run on the CPU by mistake)")
    return 0
