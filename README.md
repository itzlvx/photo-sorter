# Photo sorter

## Phase 1: date-based sorting (this folder)

Setup (Windows PowerShell):

    py -3.11 -m venv .venv
    .venv\Scripts\Activate.ps1
    pip install -r requirements.txt

Run (always test on a COPY of a small folder first):

    python sort_photos.py "C:\Photos\Test" "C:\Photos\Sorted"            # dry run, changes nothing
    python sort_photos.py "C:\Photos\Test" "C:\Photos\Sorted" --apply    # copy into Year\Month folders

Options: `--move` (verified move instead of copy), `--month-format abbr|full|number|name`, `-v` (detailed output), `--log-file FILE`.

Result: `Sorted\2023\03-mar\IMG_0001.jpg`, plus a local index at
`%LOCALAPPDATA%\photo-sorter\photo_index.db` (SQLite; records each photo's
hash, original path, sorted path, date and date source). The index is kept on
your local disk on purpose: SQLite is unreliable on network drives. Later phases add face data to this same database.

## Where is the database? Logging

The index is a SQLite file on your local disk, default
`%LOCALAPPDATA%\photo-sorter\photo_index.db`. Every run prints its exact location
at the start and end. To check it any time without running anything:

    python sort_photos.py info        # path, size, number of photos, date range

Output levels (all commands):
- Default: key steps (settings, index location, scan, progress, summary, warnings).
  Dry runs also list every file; applied runs list only progress and the summary.
- `-v`: detailed debug output with timestamps (hashes, chosen dates, name clashes,
  index writes, retries).
- `--log-file C:\path\run.log`: also saves the full debug log to a file, handy for
  long runs on a network drive. Warnings and errors go to stderr.

## Refresh mode: rebuild the index from an already-sorted library

Use this when you already have sorted folders (or the index was lost/moved to a
new PC), and before running face recognition on existing photos:

    python sort_photos.py refresh "Z:\Photos\Sorted"                  # preview
    python sort_photos.py refresh "Z:\Photos\Sorted" --apply          # update the index (also removes deleted photos)
    python sort_photos.py refresh "Z:\Photos\Sorted" --apply --keep-missing  # update, but keep rows for deleted photos

What it does:
- New files are added to the index (dated from EXIF, else file date).
- Moved or renamed files are matched by content hash and their path is updated.
- Extra copies of the same photo are reported as duplicates and never touched.
- Photos that were deleted from disk are removed from the index, together with their face
  records. The preview shows what would be removed, including how many face records go with it.
  A photo that was only moved or renamed is not "missing": it is matched by content and its path
  is updated. Use `--keep-missing` to leave rows for deleted photos in the index.
- Safety guards, because your library is on a network drive: nothing is removed if the scan finds
  no photos or hits errors (drive disconnected or unreadable), or if 10 or more photos are missing
  and that is more than half of this library's indexed photos (that looks like a disconnected or
  damaged folder, not deliberate deletions). In that case it warns and changes nothing; if you
  really did delete them, re-run with `--force-prune`.
- Only photos under the library folder you pass are considered, so refreshing one folder never
  removes photos that belong to another.
- Files whose path, size and modified time are unchanged are not re-hashed, so
  repeat refreshes of a large library are fast. `--rehash` forces a full re-hash.
  The first refresh of an index created by an older version hashes everything once.

It never moves, renames or deletes your photo files (it only updates the index). Like sorting, it is a dry run until `--apply`.
`sort_photos.py SOURCE DEST` still works as before (`sort` is the default command).

## Phase 2: find faces (offline)

The AI only finds faces and gives each one a numeric "fingerprint" (an embedding). It
never names anyone. Phase 3 groups matching fingerprints; phase 4 is where you name them.

Extra setup (once, inside the same virtual environment):

    pip install -r requirements-faces.txt

Run:

    python sort_photos.py faces                      # preview: how many photos would be scanned
    python sort_photos.py faces --apply --limit 20   # try it on 20 photos first
    python sort_photos.py faces --apply              # scan everything not yet scanned

It scans the photos listed in the index, so run `sort ... --apply` or `refresh ... --apply` first.
Useful options: `--limit N`, `--rescan` (redo everything), `--retry-errors`, `--gpu`, `--cpu`,
`--det-size 960` (finds smaller faces in group photos, slower), `--min-score`, `--max-side`,
`--model buffalo_s` (faster, a little less accurate), `--model-root`, plus `-v` and `--log-file`.

What to expect:
- The first real run downloads the model (about 280 MB) to `C:\Users\<you>\.insightface`.
  After that it works fully offline. The model is licensed for non-commercial use, which
  covers a personal photo library.
- Scanning is resumable. Every photo is saved as soon as it is done, so Ctrl+C (or a crash)
  loses nothing; run the same command again to continue. Photos that cannot be read are
  recorded as errors and skipped until you pass `--retry-errors`.
- Phone photos are rotated correctly (EXIF orientation is applied before detection).
- It uses your NVIDIA GPU when it is set up (next section) and the CPU otherwise. The log says which.
- For each face the index stores: position, detection score, size in pixels, a sharpness
  number, head turn (yaw), the 512-number embedding and a small 128 px thumbnail, so the
  naming UI never has to re-read your originals. Face fingerprints identify real people:
  keep the index on your local disk and out of cloud sync.
- `python sort_photos.py info` shows how many photos are scanned and how many faces were found.
- Photos deleted from disk are removed from the index, with their face rows, by `refresh --apply`.
  The `faces` command skips deleted photos (it does not record them as errors) and tells you to run `refresh`.

### Using your NVIDIA GPU

Face scanning picks the GPU automatically once ONNX Runtime's GPU build is installed. By default
you get the CPU build, because InsightFace installs it, and the two builds cannot be installed
together. In your virtual environment:

    pip install -r requirements-faces.txt
    pip uninstall -y onnxruntime onnxruntime-gpu
    pip install "onnxruntime-gpu[cuda,cudnn]"

The `[cuda,cudnn]` part installs the CUDA and cuDNN libraries through pip, so you do not need to
install the CUDA toolkit yourself. Then check it:

    python sort_photos.py gpucheck

`gpucheck` shows your GPU and driver, whether ONNX Runtime can really start on it, and times a
sample photo on the GPU against the CPU. After that:

    python sort_photos.py faces --apply --gpu    # --gpu stops with an explanation instead of quietly using the CPU

Good to know:
- ONNX Runtime quietly falls back to the CPU when it cannot start the GPU. The scan checks which
  hardware each model really runs on: normally it warns and carries on with the CPU, and with
  `--gpu` it refuses to run. `--cpu` forces the CPU.
- The current `onnxruntime-gpu` is built for CUDA 13. That needs an up-to-date NVIDIA driver and,
  as far as I can tell, a GTX 16-series / RTX 20-series or newer card (CUDA 13 dropped support for
  Pascal and older, such as the GTX 10-series). For an older card or driver, use the CUDA 12 build
  instead, replacing the last command above with: `pip install "onnxruntime-gpu[cuda,cudnn]==1.26.0"`.
- Windows wheels exist for Python 3.11 and 3.12.
- With a GPU, a larger `--det-size` (for example 960) costs little and finds smaller faces in
  group photos. The scan reads photos in background threads, so a network drive is usually the
  limit once the GPU is working.

## Roadmap
- Phase 2: InsightFace detection + embeddings stored in SQLite (done)
- Phase 3: clustering, with low-confidence faces flagged for review
- Phase 4: FastAPI web UI to name people and work the review queue
