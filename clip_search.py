"""Phase 7: search photos by typing a description, with a CLIP-family model run through ONNX Runtime.

`clipcheck`: before anything is indexed, it downloads (only when asked) one of the ONNX model exports that the Immich
project publishes on Hugging Face, loads it on the GPU, checks that it understands pictures and text correctly, and
times it, so you can choose a model for your hardware.

    python sort_photos.py clipcheck --list                 # suggested models, and which are downloaded
    python sort_photos.py clipcheck --download             # download the default model, then check it
    python sort_photos.py clipcheck --model ViT-B-32__openai --download

`embed`: runs every indexed photo through the picture half of a downloaded model once and stores the result (one
16-bit vector per photo per model, in photo_embeddings). Searching later only has to encode the typed text.

    python sort_photos.py embed                            # preview: how many photos would be processed
    python sort_photos.py embed --apply --limit 50         # try it on 50 photos first
    python sort_photos.py embed --apply                    # every photo without a vector for this model

A model folder holds the files below (the same layout as https://huggingface.co/immich-app/<model>):

    config.json                    embed_dim, text_cfg.context_length
    visual/model.onnx              image -> vector
    visual/preprocess_cfg.json     size, mean, std, interpolation, resize_mode, fill_color
    textual/model.onnx             token ids -> vector
    textual/tokenizer.json         Hugging Face `tokenizers` file
    textual/tokenizer_config.json  pad_token, tokenizer_class

Needs numpy, Pillow and tokenizers (pip install -r requirements-search.txt), plus the ONNX Runtime that face
scanning uses (onnxruntime-gpu for the NVIDIA GPU, or onnxruntime for the CPU).
"""
from __future__ import annotations

import io
import json
import os
import platform
import re
import shutil
import sqlite3
import string
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

import onnx_setup as ox
import photo_meta
import sort_photos as sp

log = sp.log

HF_BASE = "https://huggingface.co/immich-app"
DEFAULT_MODEL = "ViT-L-14-quickgelu__dfn2b"
SUGGESTED_MODELS = {  # name -> (approximate download size, one-line description)
    "ViT-B-32__openai": ("0.6 GB", "the original CLIP: smallest and fastest, more false matches"),
    "ViT-B-16-SigLIP2__webli": ("1.5 GB", "multilingual; strong on clean single-subject photos, needs more tuning"),
    "ViT-L-14-quickgelu__dfn2b": ("1.7 GB", "the roadmap's first choice: best on busy everyday photos, slower"),
}
MODEL_FILES = (  # small files first, so a wrong name fails before a big download starts
    "config.json",
    "visual/preprocess_cfg.json",
    "textual/tokenizer_config.json",
    "textual/tokenizer.json",
    "textual/model.onnx",
    "visual/model.onnx",
)
MODEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
CHUNK = 1 << 20
USER_AGENT = "photo-sorter"
RESAMPLE = {"bicubic": Image.Resampling.BICUBIC, "bilinear": Image.Resampling.BILINEAR,
            "nearest": Image.Resampling.NEAREST, "lanczos": Image.Resampling.LANCZOS}
COLOURS = {"red": (220, 20, 20), "green": (20, 170, 40), "blue": (20, 40, 220)}
CAMERA_PHOTO = (4032, 3024)  # a typical 12-megapixel phone photo, used to time decoding
MODEL_NOT_DOWNLOADED = 3  # exit code of clipcheck and embed when the model files are not there yet (the menu offers the download)


class ModelError(Exception):
    """The model files are missing, damaged or of a kind this program cannot use."""


# --------------------------------------------------------------------------
# Packages and model folders
# --------------------------------------------------------------------------
def runtime_problem() -> str | None:
    """Return a description of what is missing, or None if a search model can run."""
    try:
        import tokenizers  # noqa: F401
    except Exception as error:
        return f"{type(error).__name__}: {error}. Install it with: pip install -r requirements-search.txt"
    try:
        import onnxruntime  # noqa: F401
    except Exception as error:  # ImportError, or a broken native install
        return (f"{type(error).__name__}: {error}. Install ONNX Runtime the same way as for face scanning "
                "(README.md, 'Using your NVIDIA GPU'), or for the CPU only: pip install onnxruntime")
    return None


def default_model_root() -> Path:
    """Models are kept on this computer next to the index (never on the network drive)."""
    return sp.default_db_path().parent / "models"


def model_folder(root: Path | None, name: str) -> Path:
    if not MODEL_NAME.match(name) or ".." in name:
        raise ModelError(f"'{name}' is not a valid model name (letters, digits, '.', '_' and '-' only).")
    return Path(os.path.expanduser(str(root))) / name if root else default_model_root() / name


def missing_files(folder: Path) -> list[str]:
    return [rel for rel in MODEL_FILES if not (folder / rel).is_file()]


def size_hint(name: str) -> str:
    return SUGGESTED_MODELS[name][0] if name in SUGGESTED_MODELS else "several hundred MB or more"


def human_size(count: float) -> str:
    for unit in ("bytes", "KB", "MB", "GB"):
        if count < 1024 or unit == "GB":
            return f"{count:.0f} {unit}" if unit == "bytes" else f"{count:.1f} {unit}"
        count /= 1024
    return f"{count:.1f} GB"


def human_time(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f} seconds"
    if seconds < 90 * 60:
        return f"{seconds / 60:.0f} minutes"
    return f"{seconds / 3600:.1f} hours"


def download_model(name: str, folder: Path, files: list[str]) -> bool:
    """Fetch the missing files from Hugging Face. Each file is written to '<name>.part' and renamed when complete."""
    log.info("Downloading %s from %s/%s (about %s) into %s", name, HF_BASE, name, size_hint(name), folder)
    for rel in files:
        dest = folder / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")
        url = f"{HF_BASE}/{name}/resolve/main/{rel}"
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                total = int(response.headers.get("Content-Length") or 0)
                if total and shutil.disk_usage(dest.parent).free < total + 100 * CHUNK:
                    log.error("Not enough free disk space for %s (%s needed) in %s", rel, human_size(total), dest.parent)
                    return False
                log.info("  %s (%s)", rel, human_size(total) if total else "size unknown")
                done, next_report = 0, 0.25
                with open(part, "wb") as out:
                    while True:
                        chunk = response.read(CHUNK)
                        if not chunk:
                            break
                        out.write(chunk)
                        done += len(chunk)
                        if total > 50 * CHUNK and done / total >= next_report:
                            log.info("    %d%%", int(done * 100 / total))
                            next_report += 0.25
            if total and done != total:
                raise OSError(f"the download stopped early ({human_size(done)} of {human_size(total)})")
            os.replace(part, dest)
        except urllib.error.HTTPError as error:
            part.unlink(missing_ok=True)
            if error.code in (401, 404):
                log.error(
                    "The model '%s' was not found on Hugging Face (%s/%s gave %s). Check the name; "
                    "'clipcheck --list' shows the suggested ones.", name, HF_BASE, name, error.code,
                )
            else:
                log.error("Downloading %s failed: HTTP %s %s", rel, error.code, error.reason)
            return False
        except (urllib.error.URLError, OSError) as error:
            part.unlink(missing_ok=True)
            reason = getattr(error, "reason", error)
            log.error("Downloading %s failed: %s. Check the internet connection and run the same command again.", rel, reason)
            return False
        except KeyboardInterrupt:
            part.unlink(missing_ok=True)
            raise
    log.info("Download complete.")
    return True


# --------------------------------------------------------------------------
# The model
# --------------------------------------------------------------------------
def clean_text(text: str, canonicalize: bool) -> str:
    """Whitespace tidied; SigLIP-family tokenizers also expect lower case without punctuation."""
    if canonicalize:
        text = text.replace("_", " ").translate(str.maketrans("", "", string.punctuation)).lower()
    return " ".join(text.split())


def unit_rows(matrix: np.ndarray) -> np.ndarray:
    matrix = np.asarray(matrix, dtype=np.float32).reshape(len(matrix), -1)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    return matrix / np.where(norms > 0, norms, 1.0)


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ModelError(f"{path.name} cannot be read ({error}). Delete the model folder and download it again.") from error


class ClipModel:
    """The image and text halves of a CLIP-family model, with the matching preprocessing and tokenizer."""

    def __init__(self, folder: Path, providers: list[str], text: bool = True, image: bool = True):
        import onnxruntime as ort

        self.folder = folder
        config = _read_json(folder / "config.json")
        pre = _read_json(folder / "visual" / "preprocess_cfg.json") if image else {}
        size = pre.get("size", config.get("vision_cfg", {}).get("image_size", 224))
        self.height, self.width = (size, size) if isinstance(size, int) else (int(size[0]), int(size[-1]))
        self.mean = np.asarray(pre.get("mean", (0.48145466, 0.4578275, 0.40821073)), dtype=np.float32)
        self.std = np.asarray(pre.get("std", (0.26862954, 0.26130258, 0.27577711)), dtype=np.float32)
        self.resample = RESAMPLE.get(str(pre.get("interpolation", "bicubic")).lower(), Image.Resampling.BICUBIC)
        self.resize_mode = str(pre.get("resize_mode", "shortest")).lower()
        fill = pre.get("fill_color", 0)
        self.fill = tuple(fill) if isinstance(fill, (list, tuple)) else (int(fill),) * 3

        options = ort.SessionOptions()
        options.log_severity_level = 3  # errors only: hides harmless performance notes printed while loading
        try:
            self.visual = (ort.InferenceSession(str(folder / "visual" / "model.onnx"), sess_options=options, providers=providers)
                           if image else None)
            self.textual = (ort.InferenceSession(str(folder / "textual" / "model.onnx"), sess_options=options, providers=providers)
                            if text else None)
        except Exception as error:
            raise ModelError(f"the model could not be loaded: {error}") from error
        self.fixed_batch = None
        self.provider = None
        if self.visual is not None:
            image_input = self.visual.get_inputs()[0]
            self.image_input = image_input.name
            self.image_dtype = np.float16 if "float16" in image_input.type else np.float32
            first = image_input.shape[0] if image_input.shape else None
            self.fixed_batch = first if isinstance(first, int) and first > 0 else None
            self.provider = self.visual.get_providers()[0]
        self.text_provider = None
        if self.textual is not None:
            self._load_text(config, folder / "textual")

    def _load_text(self, config: dict, folder: Path) -> None:
        from tokenizers import Tokenizer

        inputs = self.textual.get_inputs()
        if len(inputs) != 1:
            raise ModelError(
                f"its text half needs {len(inputs)} inputs ({', '.join(i.name for i in inputs)}). Multilingual "
                "XLM-Roberta / NLLB models are not supported yet; choose a CLIP, DFN or SigLIP model."
            )
        self.text_input = inputs[0].name
        self.text_dtype = np.int64 if "int64" in inputs[0].type else np.int32
        shape = inputs[0].shape
        length = shape[1] if len(shape) > 1 and isinstance(shape[1], int) else config.get("text_cfg", {}).get("context_length", 77)
        tok_cfg = _read_json(folder / "tokenizer_config.json")
        pad = tok_cfg.get("pad_token") or "<|endoftext|>"
        pad = pad.get("content", "") if isinstance(pad, dict) else str(pad)
        try:
            self.tokenizer = Tokenizer.from_file(str(folder / "tokenizer.json"))
        except Exception as error:
            raise ModelError(f"tokenizer.json cannot be read ({error}). Delete the model folder and download it again.") from error
        pad_id = self.tokenizer.token_to_id(pad)
        self.tokenizer.enable_padding(length=int(length), pad_id=pad_id if pad_id is not None else 0, pad_token=pad)
        self.tokenizer.enable_truncation(max_length=int(length))
        self.canonicalize = not str(tok_cfg.get("tokenizer_class", "CLIPTokenizer")).startswith("CLIP")
        self.text_provider = self.textual.get_providers()[0]

    # ------------------------------------------------------------- pictures
    def prepare(self, image: Image.Image) -> np.ndarray:
        """A PIL image -> a (3, height, width) float array as the model expects it."""
        if getattr(image, "format", None) == "JPEG":
            image.draft("RGB", (self.width, self.height))  # decode at a reduced size: much faster for big photos
        image = ImageOps.exif_transpose(image).convert("RGB")
        w, h = self.width, self.height
        iw, ih = image.size
        if self.resize_mode == "squash":
            image = image.resize((w, h), self.resample)
        elif self.resize_mode == "longest":
            scale = min(w / iw, h / ih)
            inner = image.resize((max(1, round(iw * scale)), max(1, round(ih * scale))), self.resample)
            image = Image.new("RGB", (w, h), self.fill)
            image.paste(inner, ((w - inner.width) // 2, (h - inner.height) // 2))
        else:  # "shortest": the short side fills the square, the long side is cropped evenly
            scale = max(w / iw, h / ih)
            image = image.resize((max(w, round(iw * scale)), max(h, round(ih * scale))), self.resample)
            left, top = (image.width - w) // 2, (image.height - h) // 2
            image = image.crop((left, top, left + w, top + h))
        array = (np.asarray(image, dtype=np.float32) / 255.0 - self.mean) / self.std
        return array.transpose(2, 0, 1)

    def encode_images(self, batch: np.ndarray) -> np.ndarray:
        """(N, 3, H, W) prepared pictures -> (N, D) unit vectors."""
        step = self.fixed_batch or len(batch)
        out = [self.visual.run(None, {self.image_input: batch[i:i + step].astype(self.image_dtype)})[0]
               for i in range(0, len(batch), step)]
        return unit_rows(np.concatenate(out))

    # ------------------------------------------------------------- text
    def tokens(self, text: str) -> np.ndarray:
        return np.asarray([self.tokenizer.encode(clean_text(text, self.canonicalize)).ids], dtype=self.text_dtype)

    def encode_texts(self, texts: list[str]) -> np.ndarray:
        """Sentences -> (N, D) unit vectors."""
        return unit_rows(np.concatenate([self.textual.run(None, {self.text_input: self.tokens(t)})[0] for t in texts]))


# --------------------------------------------------------------------------
# The clipcheck command
# --------------------------------------------------------------------------
def list_models(root: Path | None) -> int:
    log.info("Suggested search models (from %s):", HF_BASE)
    for name, (size, note) in SUGGESTED_MODELS.items():
        here = "downloaded" if not missing_files(model_folder(root, name)) else "not downloaded"
        default = ", default" if name == DEFAULT_MODEL else ""
        log.info("  %-28s %-7s %s (%s%s)", name, size, note, here, default)
    base = Path(os.path.expanduser(str(root))) if root else default_model_root()
    others = sorted(p.name for p in base.iterdir() if p.is_dir() and p.name not in SUGGESTED_MODELS) if base.is_dir() else []
    for name in others:
        log.info("  %-28s also in the models folder (%s)", name, "complete" if not missing_files(base / name) else "incomplete")
    log.info("Models folder: %s", base)
    log.info("Other immich-app CLIP / SigLIP models work too: pass the name after immich-app/ with --model.")
    return 0


def synthetic_camera_jpeg() -> bytes:
    """A 12-megapixel JPEG with some detail, to time decoding the way a real photo would."""
    w, h = CAMERA_PHOTO
    small = np.random.default_rng(7).integers(0, 255, (h // 16, w // 16, 3), dtype=np.uint8)
    image = Image.fromarray(small).resize((w, h), Image.Resampling.BILINEAR)
    out = io.BytesIO()
    image.save(out, "JPEG", quality=90)
    return out.getvalue()


def timed(fn, runs: int) -> float:
    """Average seconds per call, after one warm-up call (the first is slow while the GPU sets itself up)."""
    fn()
    started = time.perf_counter()
    for _ in range(max(1, runs)):
        fn()
    return (time.perf_counter() - started) / max(1, runs)


def colour_check(model: ClipModel) -> tuple[int, list[float]]:
    """Plain red, green and blue pictures must each match their own colour's sentence best."""
    names = list(COLOURS)
    pictures = np.stack([model.prepare(Image.new("RGB", (320, 240), COLOURS[n])) for n in names])
    scores = model.encode_images(pictures) @ model.encode_texts([f"a plain {n} picture" for n in names]).T
    right, matched = 0, []
    for i, name in enumerate(names):
        order = np.argsort(-scores[i])
        best, runner_up = names[order[0]], scores[i][order[1]]
        ok = best == name
        right += ok
        matched.append(float(scores[i][i]))
        log.info("  %-5s picture -> best match '%s' (score %.3f; next best %.3f)%s",
                 name, best, scores[i][order[0]], runner_up, "" if ok else "   <-- wrong")
    return right, matched


def photo_count(db_path: Path) -> int | None:
    if not db_path.exists():
        return None
    try:
        conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            return conn.execute("SELECT COUNT(*) FROM photos").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error:
        return None


def load_model(folder: Path, mode: str, text: bool = True) -> ClipModel:
    providers, wants_gpu = ox.pick_providers(mode)  # may raise RuntimeError in 'gpu' mode
    if wants_gpu and providers[0] == ox.GPU_PROVIDER:
        problem = ox.preload_cuda_libraries()
        if problem:
            log.debug("Preloading the CUDA libraries reported: %s", problem)
    return ClipModel(folder, providers, text=text)


def clip_check(args) -> int:
    """Download (if asked), load, check and time a search-by-description model."""
    if args.list:
        return list_models(args.model_root)
    log.info("=== Search model check ===")
    try:
        folder = model_folder(args.model_root, args.model)
    except ModelError as error:
        log.error("%s", error)
        return 2
    log.info("Model: %s  (folder %s)", args.model, folder)
    log.info("Python %s on %s", platform.python_version(), platform.platform())
    smi = ox.nvidia_gpu_summary()
    if smi:
        log.info("NVIDIA GPU (name; driver; memory): %s", smi)
    else:
        log.info("No NVIDIA GPU found by nvidia-smi.")

    problem = runtime_problem()
    if problem:
        log.error("Search needs a package that is not installed: %s", problem)
        return 2
    import onnxruntime as ort

    log.info("ONNX Runtime %s; providers available: %s", ort.__version__, ", ".join(ort.get_available_providers()))

    missing = missing_files(folder)
    if missing and not args.download:
        log.error(
            "The model is not downloaded yet (%s missing). It is about %s. Download it with:\n"
            "    python sort_photos.py clipcheck --model %s --download",
            ", ".join(missing), size_hint(args.model), args.model,
        )
        return MODEL_NOT_DOWNLOADED
    if missing:
        try:
            if not download_model(args.model, folder, missing):
                return 1
        except KeyboardInterrupt:
            log.warning("Download stopped. Run the same command again to fetch the rest.")
            return 130

    mode = ox.hardware_mode(args)
    log.info("Loading the model ...")
    started = time.perf_counter()
    try:
        model = load_model(folder, mode)
    except RuntimeError as error:
        log.error("%s", error)
        return 1
    except ModelError as error:
        log.error("The model in %s cannot be used: %s", folder, error)
        return 1
    log.info("Loaded in %.1f s. Pictures run on: %s; text runs on: %s", time.perf_counter() - started,
             ox.label(model.provider), ox.label(model.text_provider))
    on_gpu = model.provider != "CPUExecutionProvider"
    if mode != "cpu" and ox.GPU_PROVIDER in ort.get_available_providers() and not on_gpu:
        log.error("The GPU is set up but the model fell back to the CPU (see the ONNX Runtime message above). %s", ox.GPU_TROUBLE)
        if mode == "gpu":
            return 1

    log.info("Checking that pictures and words line up (plain coloured pictures) ...")
    try:
        right, matched = colour_check(model)
        dim = model.encode_texts(["a dog on a beach"]).shape[1]
    except Exception as error:  # a damaged or mismatched model fails here, not later during indexing
        log.error("The model failed on a test picture: %s: %s", type(error).__name__, error)
        return 1
    if right == len(COLOURS):
        log.info("Picture/word check: %d of %d right. Scores of correct matches: %.3f to %.3f (each model has its own range).",
                 right, len(COLOURS), min(matched), max(matched))
    else:
        log.warning("Picture/word check: only %d of %d right. The model files may be damaged or not a matching pair; "
                    "delete %s and download it again.", right, len(COLOURS), folder)

    runs = max(1, args.runs)
    batch_size = model.fixed_batch or max(1, args.batch)
    log.info("Timing (%d runs each; %s) ...", runs,
             "one picture at a time, the most this model accepts" if model.fixed_batch == 1 else f"batches of {batch_size} pictures")
    photo = synthetic_camera_jpeg()
    prep = timed(lambda: model.prepare(Image.open(io.BytesIO(photo))), runs)
    log.info("  Opening and shrinking a 12-megapixel JPEG: %.0f ms (CPU work, done in parallel while indexing)", prep * 1000)
    batch = np.stack([model.prepare(Image.new("RGB", (64, 48), (i * 9 % 255, 90, 160))) for i in range(batch_size)])
    per_photo = timed(lambda: model.encode_images(batch), runs) / batch_size
    log.info("  %s: %.1f ms per picture", ox.label(model.provider), per_photo * 1000)
    per_query = timed(lambda: model.encode_texts(["birthday cake with candles"]), runs)
    log.info("  Turning a search into a vector: %.0f ms (%s); this is the wait each time you search",
             per_query * 1000, ox.label(model.text_provider))

    cpu_per_photo = None
    if on_gpu and not args.no_cpu_compare:
        log.info("  Timing the CPU for comparison ...")
        try:
            cpu = load_model(folder, "cpu", text=False)
            small = batch[:1]
            cpu_per_photo = timed(lambda: cpu.encode_images(small), max(1, runs // 2))
            log.info("  CPU: %.0f ms per picture (%.1fx slower than the GPU)", cpu_per_photo * 1000,
                     cpu_per_photo / per_photo if per_photo else 0)
            del cpu
        except Exception as error:
            log.warning("  The CPU comparison could not run: %s", error)

    count = photo_count((args.db or sp.default_db_path()))
    basis = count if count else 10_000
    fast, slow = basis * max(per_photo, prep / 3), basis * (per_photo + prep)
    log.info("--- Estimate ---")
    log.info("%s photos: about %s to %s to index (more if the photos are read from a network drive).",
             f"Your index has {basis:,}" if count else "For 10,000", human_time(fast), human_time(max(fast, slow)))
    if cpu_per_photo:
        log.info("On the CPU alone it would be about %s.", human_time(basis * (cpu_per_photo + prep / 3)))
    log.info("The stored vectors would take about %s (%d numbers per photo, 16-bit).", human_size(basis * dim * 2), dim)
    if right != len(COLOURS):
        log.warning("RESULT: the model runs but did not pass the picture/word check (see above).")
        return 1
    log.info("RESULT: %s works on %s. Try another model to compare: python sort_photos.py clipcheck --list",
             args.model, ox.label(model.provider))
    return 0


# --------------------------------------------------------------------------
# The embed command
# --------------------------------------------------------------------------
PROGRESS_EVERY = 100
MAX_FAILED_BATCHES = 3  # the model failing this many batches in a row points to a model or GPU problem


def read_picture(model: ClipModel, path: str) -> np.ndarray:
    with Image.open(path) as image:
        return model.prepare(image)


def iter_prepared(model: ClipModel, items: list, workers: int, lookahead: int = 32):
    """Yield (item, prepared picture, error) while background threads read and shrink the next photos.

    Reading photos (often from a network drive) and decoding them is the slow part, so this keeps the GPU busy
    instead of waiting on the disk.
    """
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        queue: deque = deque()
        source = iter(items)

        def submit() -> bool:
            try:
                item = next(source)
            except StopIteration:
                return False
            queue.append((item, pool.submit(read_picture, model, item[1])))
            return True

        for _ in range(max(lookahead, workers)):
            if not submit():
                break
        try:
            while queue:
                item, future = queue.popleft()
                submit()
                try:
                    yield item, future.result(), None
                except Exception as error:  # unreadable, corrupt, unsupported, missing...
                    yield item, None, error
        finally:
            for _, future in queue:  # stopping early (Ctrl+C): do not read the queued photos for nothing
                future.cancel()


def has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def pending_embeddings(conn: sqlite3.Connection, model: str, again: bool) -> list:
    """Photos that still need a vector from this model, oldest index entry first."""
    if again or not has_table(conn, "photo_embeddings"):
        return conn.execute("SELECT id, sorted_path FROM photos ORDER BY id").fetchall()
    return conn.execute(
        "SELECT p.id, p.sorted_path FROM photos p WHERE NOT EXISTS "
        "(SELECT 1 FROM photo_embeddings e WHERE e.photo_id = p.id AND e.model = ?) ORDER BY p.id",
        (model,),
    ).fetchall()


def stored_counts(conn: sqlite3.Connection) -> list[tuple[str, int, int]]:
    """(model, photos, numbers per photo) for every model that has vectors in the index."""
    if not has_table(conn, "photo_embeddings"):
        return []
    return conn.execute(
        "SELECT model, COUNT(*), MAX(dim) FROM photo_embeddings GROUP BY model ORDER BY model"
    ).fetchall()


def save_vectors(conn: sqlite3.Connection, ids: list[int], model: str, vectors: np.ndarray) -> None:
    with conn:  # one transaction per batch: a batch is either stored completely or not at all
        for photo_id, vector in zip(ids, vectors):
            photo_meta.save_embedding(conn, photo_id, model, vector, half=True)


def embed_photos(args) -> int:
    """Store one picture vector per indexed photo for a search model (resumable, dry run by default)."""
    started = time.monotonic()
    db_path = (args.db or sp.default_db_path()).resolve()
    log.info("=== Search vectors (%s) ===", "apply" if args.apply else "dry run")
    try:
        folder = model_folder(args.model_root, args.model)
    except ModelError as error:
        log.error("%s", error)
        return 2
    log.info("Model: %s  (folder %s)", args.model, folder)
    sp.log_index_location(db_path, args.apply)
    if not db_path.exists():
        log.error("There is no index yet. Run 'sort ... --apply' or 'refresh ... --apply' first, so there are photos to process.")
        return 2
    try:
        conn = sp.open_index(db_path) if args.apply else sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)
    except (OSError, sqlite3.Error) as error:
        log.error("Cannot open the index %s: %s", db_path, error)
        return 2

    try:
        total = conn.execute("SELECT COUNT(*) FROM photos").fetchone()[0]
        stored = stored_counts(conn)
        pending = pending_embeddings(conn, args.model, args.again)
    except sqlite3.Error as error:
        log.error("Cannot read the index %s: %s", db_path, error)
        conn.close()
        return 2
    done_before = next((count for name, count, _ in stored if name == args.model), 0)
    todo = pending[: args.limit] if args.limit else pending
    log.info("Photos in the index: %d | with a %s vector already: %d", total, args.model, done_before)
    for name, count, dim in stored:
        if name != args.model:
            log.info("  (also stored: %d vectors from %s, %d numbers each)", count, name, dim)
    limited = f" (limited from {len(pending)} by --limit)" if len(todo) < len(pending) else ""
    log.info("To process now: %d%s", len(todo), limited)

    missing = missing_files(folder)
    if missing:  # exit code 3 (preview too), so the menu can offer the download
        log.error("The model is not downloaded yet (%s missing). It is about %s. Download and check it with:\n"
                  "    python sort_photos.py clipcheck --model %s --download", ", ".join(missing), size_hint(args.model), args.model)
        conn.close()
        return MODEL_NOT_DOWNLOADED
    problem = runtime_problem()
    if problem:
        if args.apply and todo:
            log.error("Search needs a package that is not installed: %s", problem)
            conn.close()
            return 2
        log.warning("Search needs a package that is not installed: %s", problem)

    if not todo:
        log.info("Nothing to process.")
        conn.close()
        return 0
    if not args.apply:
        log.info("About 20 to 70 ms per photo on an NVIDIA GPU, roughly 10 to 15 times longer on a CPU, plus the time "
                 "to read the photos ('clipcheck' times your hardware).")
        log.info("Dry run only: nothing was processed or written. Re-run with --apply (try --limit 50 first).")
        conn.close()
        return 0

    mode = ox.hardware_mode(args)
    log.info("Loading the picture half of the model ...")
    try:
        model = load_model(folder, mode, text=False)
        dim = model.encode_images(np.stack([model.prepare(Image.new("RGB", (64, 48), (128, 128, 128)))])).shape[1]
    except RuntimeError as error:
        log.error("%s", error)
        conn.close()
        return 2
    except Exception as error:  # ModelError, or a damaged model failing on its first picture
        log.error("The model in %s cannot be used: %s. Run 'python sort_photos.py clipcheck --model %s' to check it.",
                  folder, error, args.model)
        conn.close()
        return 2
    log.info("Model ready on %s (%.1f s); %d numbers per photo", ox.label(model.provider), time.monotonic() - started, dim)
    import onnxruntime as ort

    if mode != "cpu" and ox.GPU_PROVIDER in ort.get_available_providers() and model.provider == "CPUExecutionProvider":
        log.warning("The GPU is set up but the model fell back to the CPU, so this will be much slower. %s", ox.GPU_TROUBLE)
    clash = next((d for name, _, d in stored if name == args.model and d != dim), None)
    if clash and not args.again:
        log.error("The index already holds %s vectors with %d numbers each, but this model makes %d. The model files "
                  "have changed; run again with --again to replace all of them.", args.model, clash, dim)
        conn.close()
        return 2

    batch_size = model.fixed_batch or max(1, args.batch)
    stats = {"done": 0, "missing": 0, "unreadable": 0, "lost": 0}
    state = {"failed_batches": 0, "aborted": None}
    batch_ids: list[int] = []
    batch_pictures: list[np.ndarray] = []
    interrupted = False
    loop_started = time.monotonic()

    def flush() -> None:
        if not batch_ids:
            return
        try:
            try:
                vectors = model.encode_images(np.stack(batch_pictures))
            except Exception as exc:
                state["failed_batches"] += 1
                stats["lost"] += len(batch_ids)
                log.warning("The model failed on %d photo(s) (ids %d to %d): %s: %s", len(batch_ids), batch_ids[0],
                            batch_ids[-1], type(exc).__name__, exc)
                if state["failed_batches"] >= MAX_FAILED_BATCHES:
                    state["aborted"] = (f"the model failed on {state['failed_batches']} batches in a row, which points "
                                        "to a problem with the model or GPU (try --cpu, or run clipcheck)")
                return
            try:
                save_vectors(conn, batch_ids, args.model, vectors)
            except (sqlite3.Error, ValueError) as exc:
                state["aborted"] = f"could not write to the index ({exc}); stopping so no work is lost silently"
                return
            state["failed_batches"] = 0
            before = stats["done"]
            stats["done"] += len(batch_ids)
            if stats["done"] // PROGRESS_EVERY > before // PROGRESS_EVERY:
                elapsed = time.monotonic() - loop_started
                rate = stats["done"] / elapsed if elapsed else 0.0
                left = (len(todo) - stats["done"]) / rate if rate else 0.0
                log.info("Progress: %d/%d photos, %.1f photos/s, about %s left", stats["done"], len(todo), rate,
                         human_time(left))
        finally:
            batch_ids.clear()
            batch_pictures.clear()

    log.info("Processing %d photos, %s (Ctrl+C stops safely; progress is saved after every batch) ...", len(todo),
             "one at a time" if batch_size == 1 else f"{batch_size} at a time")
    try:
        for (photo_id, path), picture, error in iter_prepared(model, list(todo), args.workers):
            if isinstance(error, FileNotFoundError):
                stats["missing"] += 1  # deleted, or its drive is offline: 'refresh' removes it from the index
                log.warning("File no longer exists, skipped: %s", path)
                continue
            if error is not None:
                stats["unreadable"] += 1
                log.warning("Could not read %s: %s: %s", path, type(error).__name__, error)
                continue
            batch_ids.append(photo_id)
            batch_pictures.append(picture)
            if len(batch_ids) >= batch_size:
                flush()
                if state["aborted"]:
                    break
        if not state["aborted"]:
            flush()
    except KeyboardInterrupt:
        interrupted = True
        log.warning("Stopped by you. Everything processed so far is saved; run the same command again to continue.")

    aborted = state["aborted"]
    elapsed = time.monotonic() - loop_started
    held = conn.execute("SELECT COUNT(*) FROM photo_embeddings WHERE model = ?", (args.model,)).fetchone()[0]
    conn.close()
    log.info("--- Search vectors summary ---")
    log.info("%-22s%d of %d", "Photos processed:", stats["done"], len(todo))
    if stats["unreadable"]:
        log.info("%-22s%d (tried again next time)", "  could not be read:", stats["unreadable"])
    if stats["lost"]:
        log.info("%-22s%d (tried again next time)", "  model failed on:", stats["lost"])
    if stats["done"] and elapsed:
        log.info("%-22s%.1f photos/s", "Speed:", stats["done"] / elapsed)
    log.info("%-22s%d of %d photos have a %s vector", "Index now holds:", held, total, args.model)
    if stats["missing"]:
        log.warning("%d photos no longer exist on disk and were skipped (is the drive connected?). If they were "
                    "deleted, run 'sort_photos.py refresh <library> --apply' to remove them from the index.",
                    stats["missing"])
    if aborted:
        log.error("Stopped early: %s", aborted)
    log.info("Finished in %s", human_time(time.monotonic() - started))
    if not aborted and not interrupted and len(todo) < len(pending):
        log.info("%d more photos still to process; run the command again to continue.", len(pending) - len(todo))
    if interrupted:
        return 130  # the conventional exit code for Ctrl+C
    return 1 if aborted or stats["unreadable"] or stats["lost"] else 0


# --------------------------------------------------------------------------
# Searching by description (used by the web interface)
# --------------------------------------------------------------------------
TEXT_FILES = ("config.json", "textual/tokenizer_config.json", "textual/tokenizer.json", "textual/model.onnx")
SEARCH_LIMITS = (50, 100, 200, 500, 1000)
SCORE_CHUNK = 20_000   # rows compared at a time, so the 16-bit vectors never need a full 32-bit copy
MAX_QUERY = 300


class SearchError(Exception):
    """A problem that stops a search; the message is shown to the person as it is."""


class PhotoSearch:
    """Ranks the photos of the index by how well they match a typed description.

    Only the text half of a model is loaded (on the CPU by default: about 0.1 s per search, and the GPU stays free for
    face scanning and 'embed'). The stored vectors of each model are kept in memory as 16-bit numbers and reloaded only
    when they change in the index.
    """

    def __init__(self, model_root: Path | None = None, providers: list[str] | None = None):
        self.root = model_root
        self.providers = providers or ["CPUExecutionProvider"]
        self._lock = threading.Lock()
        self._models: dict[str, ClipModel] = {}
        self._vectors: dict[str, tuple] = {}      # model -> (version key, ids, float16 matrix)
        self._ranked: dict[tuple, tuple] = {}     # (model, text, version key) -> (ids, scores), the last few searches

    # ---------------------------------------------------------------- models
    def problem(self, name: str) -> str | None:
        """Why this model cannot be used for searching, or None."""
        try:
            folder = model_folder(self.root, name)
        except ModelError as error:
            return str(error)
        missing = [f for f in TEXT_FILES if not (folder / f).is_file()]
        if missing:
            return (f"The {name} search model is not on this computer ({', '.join(missing)} missing). Download it again "
                    f"with: python sort_photos.py clipcheck --model {name} --download")
        trouble = runtime_problem()
        if trouble:
            return f"Searching by description needs a package that is not installed: {trouble}"
        return None

    def stored_models(self, conn: sqlite3.Connection) -> list[dict]:
        """Every model with vectors in the index, most photos first, and whether it can be used to search."""
        rows = sorted(stored_counts(conn), key=lambda r: (-r[1], r[0] != DEFAULT_MODEL, r[0]))
        out = []
        for name, count, dim in rows:
            problem = self.problem(name)
            out.append({"name": name, "photos": count, "dim": dim, "ready": problem is None, "problem": problem or ""})
        return out

    def text_model(self, name: str) -> ClipModel:
        with self._lock:
            model = self._models.get(name)
            if model is None:
                problem = self.problem(name)
                if problem:
                    raise SearchError(problem)
                started = time.perf_counter()
                try:
                    model = ClipModel(model_folder(self.root, name), self.providers, text=True, image=False)
                except ModelError as error:
                    raise SearchError(f"The {name} search model cannot be used: {error}") from error
                log.info("Search model %s ready on %s (%.1f s)", name, ox.label(model.text_provider), time.perf_counter() - started)
                self._models[name] = model
            return model

    # --------------------------------------------------------------- vectors
    def _version(self, conn: sqlite3.Connection, name: str) -> tuple:
        return tuple(conn.execute(
            "SELECT COUNT(*), MAX(rowid), SUM(photo_id), MAX(created_at) FROM photo_embeddings WHERE model = ?", (name,)
        ).fetchone())

    def vectors(self, conn: sqlite3.Connection, name: str) -> tuple[tuple, np.ndarray, np.ndarray]:
        version = self._version(conn, name)
        with self._lock:
            cached = self._vectors.get(name)
            if cached and cached[0] == version:
                return cached
        ids, rows = [], []
        for photo_id, dim, blob in conn.execute(
            "SELECT photo_id, dim, vector FROM photo_embeddings WHERE model = ? ORDER BY photo_id", (name,)
        ):
            if dim and len(blob) == dim * 2:
                rows.append(np.frombuffer(blob, dtype=np.float16))
            elif dim and len(blob) == dim * 4:
                rows.append(np.frombuffer(blob, dtype=np.float32).astype(np.float16))
            else:
                continue  # a damaged row: that photo is simply not found until 'embed --again'
            ids.append(photo_id)
        if rows and len({r.size for r in rows}) > 1:
            raise SearchError(f"The stored {name} vectors have different sizes. Run: python sort_photos.py embed --apply --again --model {name}")
        entry = (version, np.asarray(ids, dtype=np.int64),
                 np.stack(rows) if rows else np.zeros((0, 0), dtype=np.float16))
        with self._lock:
            self._vectors[name] = entry
        return entry

    # ---------------------------------------------------------------- search
    def rank(self, conn: sqlite3.Connection, name: str, text: str) -> tuple[np.ndarray, np.ndarray]:
        """(photo ids, scores), best match first, for every photo that has a vector from this model."""
        text = " ".join(text.split())[:MAX_QUERY]
        if not text:
            raise SearchError("Type a description to search for.")
        version, ids, matrix = self.vectors(conn, name)
        key = (name, text, version)
        with self._lock:
            if key in self._ranked:
                return self._ranked[key]
        model = self.text_model(name)
        query = model.encode_texts([text])[0].astype(np.float32)
        if len(ids) and matrix.shape[1] != query.size:
            raise SearchError(f"The stored {name} vectors have {matrix.shape[1]} numbers each but the model makes {query.size}. "
                              f"Run: python sort_photos.py embed --apply --again --model {name}")
        scores = (np.concatenate([matrix[i:i + SCORE_CHUNK].astype(np.float32) @ query for i in range(0, len(ids), SCORE_CHUNK)])
                  if len(ids) else np.zeros(0, dtype=np.float32))
        order = np.argsort(-scores, kind="stable")
        result = (ids[order], scores[order])
        with self._lock:
            if len(self._ranked) >= 8:
                self._ranked.pop(next(iter(self._ranked)))
            self._ranked[key] = result
        return result
