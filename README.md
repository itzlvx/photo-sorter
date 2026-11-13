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
    python sort_photos.py refresh "Z:\Photos\Sorted" --apply          # update the index
    python sort_photos.py refresh "Z:\Photos\Sorted" --apply --prune  # also drop rows for deleted files

What it does:
- New files are added to the index (dated from EXIF, else file date).
- Moved or renamed files are matched by content hash and their path is updated.
- Extra copies of the same photo are reported as duplicates and never touched.
- Files that no longer exist are reported; they leave the index only with `--prune`.
  Pruning is skipped automatically if the scan finds no photos or hits errors
  (for example, the network drive is disconnected).
- Files whose path, size and modified time are unchanged are not re-hashed, so
  repeat refreshes of a large library are fast. `--rehash` forces a full re-hash.
  The first refresh of an index created by an older version hashes everything once.

It never moves, renames or deletes photos. Like sorting, it is a dry run until `--apply`.
`sort_photos.py SOURCE DEST` still works as before (`sort` is the default command).

## Roadmap
- Phase 2: InsightFace detection + embeddings stored in SQLite
- Phase 3: clustering, with low-confidence faces flagged for review
- Phase 4: FastAPI web UI to name people and work the review queue
