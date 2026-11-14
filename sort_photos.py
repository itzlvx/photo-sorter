#!/usr/bin/env python3
"""Photo sorter: sort photos into Year/Month folders by date taken, and keep a
local SQLite index of every photo.

Everything is a dry run until you pass --apply.

    python sort_photos.py SOURCE DEST            # preview ('sort' is the default command)
    python sort_photos.py SOURCE DEST --apply    # copy photos into DEST
    python sort_photos.py SOURCE DEST --apply --move

Refresh mode rebuilds/updates the index from a library that is already sorted
(also the starting point for face recognition on existing photos):

    python sort_photos.py refresh LIBRARY            # preview what would change
    python sort_photos.py refresh LIBRARY --apply    # update the index; photos deleted from
                                                     # disk are removed from it (with their faces)
    python sort_photos.py refresh LIBRARY --apply --keep-missing   # do NOT remove rows for deleted files

Show where the index database is, and what is in it:

    python sort_photos.py info

Read camera details (make, model, lens, exposure, size, GPS) for photos indexed before they
were recorded (sort and refresh record them for new photos; refresh also fills the gaps):

    python sort_photos.py metadata --apply

Find faces in the indexed photos (phase 2; needs requirements-faces.txt):

    python sort_photos.py faces                      # preview: how many photos would be scanned
    python sort_photos.py faces --apply --limit 20   # try it on 20 photos first
    python sort_photos.py faces --apply              # scan every photo not yet scanned

Check that the NVIDIA GPU is really being used, and how much faster it is:

    python sort_photos.py gpucheck

Logging: key steps are always printed. Add -v for detailed (debug) output and
--log-file FILE to also save a full debug log to a file.

Date priority: EXIF DateTimeOriginal > DateTimeDigitized > DateTime >
file system date (earliest of creation/modified time on Windows, modified
time elsewhere). Every photo records which source its date came from.

Duplicates are detected by SHA-256 content hash, both within a run and
against earlier runs (via the local index, see --db). Duplicate files are
reported and left untouched.
"""
from __future__ import annotations

import argparse
import hashlib
import logging
import os
import shutil
import sqlite3
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

from PIL import Image

import photo_meta  # phase 6: camera details, notes/tags, embeddings, search index

try:  # HEIC/HEIF support (iPhone photos)
    from pillow_heif import register_heif_opener

    register_heif_opener()
except ImportError:
    pass

TRASH_DIR = ".photo-sorter-trash"  # photos deleted in the web interface are moved here, never scanned
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".tif", ".tiff", ".webp"}
MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]
MONTH_ABBR = [
    "jan", "feb", "mar", "apr", "may", "jun",
    "jul", "aug", "sep", "oct", "nov", "dec",
]
EXIF_IFD = 0x8769  # pointer to the Exif sub-directory
TAG_DATETIME_ORIGINAL = 0x9003
TAG_DATETIME_DIGITIZED = 0x9004
TAG_DATETIME = 0x0132
DB_NAME = "photo_index.db"
PROGRESS_EVERY = 200
# Safety net for removing missing photos: if at least this many photos are missing AND they are
# more than half of this library's indexed photos, it is probably a disconnected or damaged folder.
MASS_MISSING_MIN = 10
MASS_MISSING_FRACTION = 0.5

SCHEMA = """
CREATE TABLE IF NOT EXISTS photos (
    id            INTEGER PRIMARY KEY,
    sha256        TEXT NOT NULL UNIQUE,
    original_path TEXT NOT NULL,
    sorted_path   TEXT NOT NULL,
    taken_at      TEXT NOT NULL,
    date_source   TEXT NOT NULL,
    size_bytes    INTEGER NOT NULL,
    sorted_at     TEXT NOT NULL,
    mtime_ns      INTEGER
)
"""

FACE_SCHEMA = [
    # One row per photo that has been through face detection (even if no face was found).
    """CREATE TABLE IF NOT EXISTS face_scans (
        photo_id   INTEGER PRIMARY KEY REFERENCES photos(id) ON DELETE CASCADE,
        scanned_at TEXT NOT NULL,
        model      TEXT NOT NULL,
        face_count INTEGER NOT NULL,
        error      TEXT
    )""",
    # One row per detected face. The box is 0..1 of the upright (EXIF-rotated) photo.
    # embedding = 512 float32 values (2048 bytes), unit length; thumb = small JPEG.
    """CREATE TABLE IF NOT EXISTS faces (
        id        INTEGER PRIMARY KEY,
        photo_id  INTEGER NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
        x1 REAL NOT NULL, y1 REAL NOT NULL, x2 REAL NOT NULL, y2 REAL NOT NULL,
        det_score REAL NOT NULL,
        face_px   INTEGER NOT NULL,
        blur      REAL,
        yaw       REAL,
        embedding BLOB NOT NULL,
        thumb     BLOB,
        model     TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_faces_photo ON faces(photo_id)",
    "CREATE INDEX IF NOT EXISTS idx_photos_taken ON photos(taken_at)",  # phase 5: browsing by date
    # Phase 3: people. A person is just a group of faces the AI thinks belong together.
    # name stays NULL until you name the group in the web UI (phase 4).
    """CREATE TABLE IF NOT EXISTS persons (
        id         INTEGER PRIMARY KEY,
        name       TEXT,
        created_at TEXT NOT NULL
    )""",
    # Which person each face belongs to. status:
    #   auto        the AI is confident
    #   review      the AI is unsure; person_id (if set) is only its best guess. Shown to you in the UI.
    #   unassigned  no good match (usually a one-off face); not shown in the review queue
    #   confirmed   you checked it (set by the UI, never changed by the AI)
    #   ignored     you dismissed it in the UI (not a face, a stranger...); the AI leaves it alone
    # reason says why a face is not 'auto' (low_quality, borderline, ambiguous, no_match, ...).
    """CREATE TABLE IF NOT EXISTS face_person (
        face_id     INTEGER PRIMARY KEY REFERENCES faces(id) ON DELETE CASCADE,
        person_id   INTEGER REFERENCES persons(id) ON DELETE SET NULL,
        similarity  REAL,
        status      TEXT NOT NULL,
        reason      TEXT,
        assigned_at TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_face_person_person ON face_person(person_id)",
    "CREATE INDEX IF NOT EXISTS idx_face_person_status ON face_person(status)",
    # "This face is NOT this person" answers from the UI, so the AI never suggests them again.
    """CREATE TABLE IF NOT EXISTS face_rejections (
        face_id   INTEGER NOT NULL REFERENCES faces(id) ON DELETE CASCADE,
        person_id INTEGER NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
        PRIMARY KEY (face_id, person_id)
    )""",
]

log = logging.getLogger("photo_sorter")


# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------
class ConsoleFormatter(logging.Formatter):
    """Plain messages normally; timestamps and levels when -v is used."""

    def __init__(self, verbose: bool) -> None:
        super().__init__()
        self.verbose = verbose

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        if self.verbose:
            return f"{self.formatTime(record, '%H:%M:%S')} {record.levelname:<7} {message}"
        if record.levelno >= logging.WARNING:
            return f"{record.levelname}: {message}"
        return message


class _BelowWarning(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno < logging.WARNING


def setup_logging(verbose: bool = False, log_file: Path | None = None) -> None:
    log.handlers.clear()
    log.setLevel(logging.DEBUG)
    log.propagate = False
    formatter = ConsoleFormatter(verbose)

    out = logging.StreamHandler(sys.stdout)  # normal messages
    out.setLevel(logging.DEBUG if verbose else logging.INFO)
    out.addFilter(_BelowWarning())
    out.setFormatter(formatter)
    log.addHandler(out)

    err = logging.StreamHandler(sys.stderr)  # warnings and errors
    err.setLevel(logging.WARNING)
    err.setFormatter(formatter)
    log.addHandler(err)

    if log_file:
        try:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            handler = logging.FileHandler(log_file, encoding="utf-8")
        except OSError as error:
            log.warning("Cannot open log file %s: %s", log_file, error)
        else:
            handler.setLevel(logging.DEBUG)  # the file always gets full detail
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
            log.addHandler(handler)
            log.info("Saving a full debug log to: %s", log_file)


def human_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{num_bytes} B"


# --------------------------------------------------------------------------
# Dates
# --------------------------------------------------------------------------
def parse_exif_date(value) -> datetime | None:
    """Parse an EXIF date string; return None for blank or bogus values."""
    if not value:
        return None
    if isinstance(value, bytes):
        value = value.decode(errors="ignore")
    text = str(value).strip().strip("\x00")[:19]
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if parsed.year >= 1900:
            return parsed
    return None


def read_exif_details(path: Path) -> tuple[tuple[datetime, str] | None, dict | None, str | None]:
    """Open the photo once: (EXIF date and its source or None, camera details or None, error or None)."""
    try:
        with Image.open(path) as img:
            exif = img.getexif()
            exif_ifd = exif.get_ifd(EXIF_IFD)
            candidates = (
                (exif_ifd.get(TAG_DATETIME_ORIGINAL), "exif_original"),
                (exif_ifd.get(TAG_DATETIME_DIGITIZED), "exif_digitized"),
                (exif.get(TAG_DATETIME), "exif_modified"),
            )
            fields = photo_meta.exif_fields(img)
    except Exception as error:  # corrupt or unusual files raise many different errors
        log.debug("Could not read EXIF from %s: %s", path.name, error)
        return None, None, (str(error) or type(error).__name__)[:300]
    for raw, label in candidates:
        parsed = parse_exif_date(raw)
        if parsed:
            return (parsed, label), fields, None
    return None, fields, None


def read_exif_date(path: Path) -> tuple[datetime, str] | None:
    return read_exif_details(path)[0]


def file_system_date(path: Path) -> datetime:
    stat = path.stat()
    timestamp = stat.st_mtime
    if os.name == "nt":  # on Windows st_ctime is the creation time
        timestamp = min(timestamp, stat.st_ctime)
    return datetime.fromtimestamp(timestamp)


def get_taken_date(path: Path) -> tuple[datetime, str]:
    return read_exif_date(path) or (file_system_date(path), "file_date")


def read_photo_details(path: Path) -> tuple[datetime, str, dict | None, str | None]:
    """Date taken, where the date came from, camera details and a read error, from one opening of the file."""
    found, fields, error = read_exif_details(path)
    taken, date_source = found or (file_system_date(path), "file_date")
    return taken, date_source, fields, error


# --------------------------------------------------------------------------
# Files and hashing
# --------------------------------------------------------------------------
def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def iter_photos(source: Path, exclude: Path | None = None, onerror=None):
    """Yield image files under source, optionally skipping one folder."""
    for root, dirs, files in os.walk(source, onerror=onerror):
        root_path = Path(root)
        dirs[:] = [d for d in dirs if d != TRASH_DIR]
        if exclude is not None:
            dirs[:] = [d for d in dirs if (root_path / d).resolve() != exclude]
        for name in sorted(files):
            path = root_path / name
            if path.suffix.lower() in IMAGE_EXTS:
                yield path


def month_folder(month: int, style: str) -> str:
    if style == "number":
        return f"{month:02d}"
    if style == "name":
        return MONTH_NAMES[month - 1]
    if style == "full":
        return f"{month:02d}-{MONTH_NAMES[month - 1].lower()}"
    return f"{month:02d}-{MONTH_ABBR[month - 1]}"  # "abbr": 01-jan, 02-feb, ...


def choose_destination(
    folder: Path, name: str, sha: str, reserved: set[Path]
) -> tuple[Path, bool]:
    """Pick a free filename in folder. Returns (path, is_duplicate).

    If a file with this name already exists and has identical content, the
    photo is already sorted, so it is flagged as a duplicate.
    """
    candidate = folder / name
    stem, suffix = candidate.stem, candidate.suffix
    counter = 1
    while candidate.exists() or candidate in reserved:
        if candidate.exists() and sha256_of(candidate) == sha:
            return candidate, True
        candidate = folder / f"{stem}_{counter}{suffix}"
        counter += 1
    return candidate, False


def transfer(src: Path, target: Path, move: bool, sha: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, target)
    if move:
        # Only delete the original after proving the copy is identical.
        if sha256_of(target) != sha:
            target.unlink(missing_ok=True)
            raise OSError("copy verification failed; original left in place")
        src.unlink()
        log.debug("Verified the copy, then deleted the original: %s", src)


# --------------------------------------------------------------------------
# Index database
# --------------------------------------------------------------------------
def default_db_path() -> Path:
    """Index lives on the local disk: SQLite is unreliable on network shares."""
    base = os.environ.get("LOCALAPPDATA") or (Path.home() / ".local" / "share")
    return Path(base) / "photo-sorter" / DB_NAME


def is_network_path(path: Path) -> bool:
    if os.name != "nt":
        return False
    text = str(path)
    if text.startswith("\\\\?\\UNC\\") or (
        text.startswith("\\\\") and not text.startswith("\\\\?\\")
    ):
        return True
    try:
        import ctypes

        return bool(path.drive) and ctypes.windll.kernel32.GetDriveTypeW(path.drive + "\\") == 4
    except Exception:
        return False


def log_index_location(db_path: Path, apply: bool) -> None:
    """Always tell the user exactly where the index database is."""
    if db_path.exists():
        log.info("Index database: %s (%s)", db_path, human_size(db_path.stat().st_size))
    elif apply:
        log.info("Index database: %s (new, will be created)", db_path)
    else:
        log.info("Index database: %s (does not exist yet; a dry run does not create it)", db_path)
    if is_network_path(db_path):
        log.warning(
            "The index %s is on a network drive. SQLite can fail there; use a local path with --db.",
            db_path,
        )


def load_known_hashes(db_path: Path) -> set[str]:
    if not db_path.exists():
        return set()
    conn = sqlite3.connect(db_path)
    try:
        return {row[0] for row in conn.execute("SELECT sha256 FROM photos")}
    except sqlite3.OperationalError:
        return set()
    finally:
        conn.close()


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(SCHEMA)
    for statement in FACE_SCHEMA:
        conn.execute(statement)
    photo_meta.ensure_meta_schema(conn)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(photos)")}
    if "mtime_ns" not in columns:  # index created by an older version of this script
        log.info("Upgrading the index to the current format (adding mtime_ns)")
        conn.execute("ALTER TABLE photos ADD COLUMN mtime_ns INTEGER")
    person_columns = {row[1] for row in conn.execute("PRAGMA table_info(persons)")}
    if "locked" not in person_columns:  # phase 4: people you merged or edited are protected from regrouping
        conn.execute("ALTER TABLE persons ADD COLUMN locked INTEGER NOT NULL DEFAULT 0")
    conn.commit()


def open_index(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30)
    # Keep the rollback journal in memory so SQLite never has to create a
    # separate *.db-journal file (OneDrive/antivirus can block that on Windows).
    conn.execute("PRAGMA journal_mode=MEMORY")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")  # deleting a photo row also deletes its faces
    ensure_schema(conn)
    log.debug("Opened the index %s", db_path)
    return conn


def record_photo(conn, sha, src, target, taken, date_source, fields=None, read_error=None) -> None:
    stat = target.stat()
    row = (
        sha,
        str(src),
        str(target),
        taken.isoformat(timespec="seconds"),
        date_source,
        stat.st_size,
        datetime.now().isoformat(timespec="seconds"),
        stat.st_mtime_ns,
    )
    for attempt in range(5):  # retry: sync clients/antivirus can lock the file briefly
        try:
            conn.execute(
                "INSERT OR IGNORE INTO photos (sha256, original_path, sorted_path,"
                " taken_at, date_source, size_bytes, sorted_at, mtime_ns) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                row,
            )
            conn.execute(photo_meta.SAVE_EXIF_BY_SHA, (photo_meta.now(), *photo_meta.exif_values(fields), read_error, sha))
            conn.commit()
            return
        except sqlite3.OperationalError as error:
            if attempt == 4:
                raise
            log.debug("Index write failed (%s); retrying (attempt %d of 5)", error, attempt + 2)
            time.sleep(0.5 * (attempt + 1))


def load_index_rows(conn: sqlite3.Connection) -> list[dict]:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(photos)")}
    mtime_col = "mtime_ns" if "mtime_ns" in columns else "NULL"
    query = f"SELECT id, sha256, sorted_path, size_bytes, {mtime_col} FROM photos ORDER BY id"
    return [
        {"id": r[0], "sha": r[1], "path": r[2], "size": r[3], "mtime": r[4]}
        for r in conn.execute(query)
    ]


# --------------------------------------------------------------------------
# sort command
# --------------------------------------------------------------------------
def run(args: argparse.Namespace) -> int:
    started = time.monotonic()
    source = args.source.resolve()
    dest = args.dest.resolve()
    if not source.is_dir():
        log.error("Source folder not found: %s", source)
        return 2
    if source == dest:
        log.error("Source and destination must be different folders.")
        return 2

    verb = "MOVE" if args.move else "COPY"
    db_path = (args.db or default_db_path()).resolve()
    log.info("=== Sort started (%s, %s) ===", "apply" if args.apply else "dry run", "move" if args.move else "copy")
    log.info("Source:        %s", source)
    log.info("Destination:   %s", dest)
    log.info("Month folders: %s (for example %s)", args.month_format, month_folder(3, args.month_format))
    log_index_location(db_path, args.apply)

    conn = None
    if args.apply:
        try:
            dest.mkdir(parents=True, exist_ok=True)
            db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = open_index(db_path)
        except (OSError, sqlite3.Error) as error:
            log.error("Cannot open the index %s: %s", db_path, error)
            return 2

    seen = load_known_hashes(db_path)
    log.info("The index already knows %d photos", len(seen))
    reserved: set[Path] = set()
    stats: Counter[str] = Counter()
    detail = log.debug if args.apply else log.info  # per-file lines: preview always, apply with -v

    log.info("Scanning %s ...", source)
    for src in iter_photos(source, exclude=dest):
        stats["scanned"] += 1
        try:
            sha = sha256_of(src)
            log.debug("Hashed %s -> %s", src.name, sha[:12])
            if sha in seen:
                stats["duplicates"] += 1
                detail("SKIP (duplicate)  %s", src)
                continue

            taken, date_source, fields, read_error = read_photo_details(src)
            log.debug("Date for %s: %s (from %s)", src.name, taken.isoformat(timespec="seconds"), date_source)
            folder = dest / f"{taken.year:04d}" / month_folder(taken.month, args.month_format)
            target, is_duplicate = choose_destination(folder, src.name, sha, reserved)
            if is_duplicate:
                stats["duplicates"] += 1
                detail("SKIP (already sorted)  %s", src)
                continue
            if target.name != src.name:
                log.debug("Name clash: %s will be saved as %s", src.name, target.name)

            if args.apply:
                transfer(src, target, args.move, sha)
                log.debug("%s complete: %s", verb.title(), target)
                try:
                    record_photo(conn, sha, src, target, taken, date_source, fields, read_error)
                    log.debug("Index row written for %s", target.name)
                except sqlite3.Error as error:
                    stats["index_errors"] += 1
                    if stats["index_errors"] == 1:
                        log.warning(
                            "Could not write to the index %s: %s. Photos are still being sorted, "
                            "but the index will be incomplete.",
                            db_path, error,
                        )
            seen.add(sha)
            reserved.add(target)
            stats["sorted"] += 1
            stats[f"date:{date_source}"] += 1
            detail("%s  %s  ->  %s  [%s]", verb if args.apply else f"WOULD {verb}", src, target, date_source)
        except OSError as error:
            stats["errors"] += 1
            log.error("%s: %s", src, error)

        if args.apply and stats["scanned"] % PROGRESS_EVERY == 0:
            log.info(
                "Progress: %d scanned, %d sorted, %d duplicates",
                stats["scanned"], stats["sorted"], stats["duplicates"],
            )

    if conn:
        conn.close()

    log.info("--- Summary ---")
    log.info("%-22s%d", "Photos scanned:", stats["scanned"])
    log.info("%-22s%d", "Photos sorted:" if args.apply else "Would sort:", stats["sorted"])
    log.info("%-22s%d", "Duplicates skipped:", stats["duplicates"])
    log.info("%-22s%d", "Errors:", stats["errors"])
    if stats["index_errors"]:
        log.warning("Index write failures: %d (try --db on a local folder)", stats["index_errors"])
    for key in sorted(k for k in stats if k.startswith("date:")):
        log.info("  dated by %s: %d", key[5:], stats[key])
    if stats["date:file_date"]:
        log.info("  (file_date means no EXIF date was found; those may be worth a look)")
    log_index_location(db_path, args.apply)
    log.info("Finished in %.1f seconds", time.monotonic() - started)
    if not args.apply:
        log.info("Dry run only: nothing was changed. Re-run with --apply to sort for real.")
    return 1 if stats["errors"] else 0


def exif_ids(conn: sqlite3.Connection) -> set[int]:
    """Ids of photos whose camera details were already read."""
    try:
        return {row[0] for row in conn.execute("SELECT photo_id FROM photo_exif")}
    except sqlite3.Error:
        return set()


def exif_ids_read_only(db_path: Path) -> set[int]:
    if not db_path.exists():
        return set()
    try:
        conn = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)
    except sqlite3.Error:
        return set()
    try:
        return exif_ids(conn)
    finally:
        conn.close()


def count_faces_for(db_path: Path, photo_ids: list[int]) -> int:
    """How many stored face records belong to these photos (0 if there are none or no face table)."""
    if not photo_ids or not db_path.exists():
        return 0
    try:
        conn = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)
        try:
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='faces'").fetchone():
                return 0
            total = 0
            for start in range(0, len(photo_ids), 500):  # stay under SQLite's variable limit
                chunk = photo_ids[start:start + 500]
                marks = ",".join("?" * len(chunk))
                total += conn.execute(f"SELECT COUNT(*) FROM faces WHERE photo_id IN ({marks})", chunk).fetchone()[0]
            return total
        finally:
            conn.close()
    except sqlite3.Error:
        return 0


# --------------------------------------------------------------------------
# refresh command: rebuild/refresh the index from an already-sorted library
# --------------------------------------------------------------------------
def _norm(path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def refresh(args: argparse.Namespace) -> int:
    """Walk an already-sorted library and bring the index in line with the disk.

    - new files          -> added to the index (dated from EXIF/file date)
    - moved/renamed      -> path updated (matched by content hash)
    - extra copies       -> reported as duplicates, never touched
    - files that are gone -> removed from the index together with their face records
                            (unless --keep-missing; guarded against disconnected drives)
    Files whose path, size and modified time are unchanged are not re-hashed,
    so repeat refreshes of a big library are fast (use --rehash to force).
    """
    started = time.monotonic()
    library = args.library.resolve()
    if not library.is_dir():
        log.error("Library folder not found: %s", library)
        return 2
    db_path = (args.db or default_db_path()).resolve()
    log.info("=== Refresh started (%s) ===", "apply" if args.apply else "dry run")
    log.info("Library:  %s", library)
    log.info("Options:  remove missing photos from the index=%s, rehash=%s", not args.keep_missing, args.rehash)
    log_index_location(db_path, args.apply)

    conn = None
    rows: list[dict] = []
    if args.apply:
        try:
            db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = open_index(db_path)
            rows = load_index_rows(conn)
        except (OSError, sqlite3.Error) as error:
            log.error("Cannot open the index %s: %s", db_path, error)
            return 2
    elif db_path.exists():
        try:
            read_only = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)
            try:
                rows = load_index_rows(read_only)
            finally:
                read_only.close()
        except sqlite3.Error as error:
            log.debug("Could not read the existing index (%s); treating it as empty", error)
            rows = []

    root = _norm(library)
    original_rows = list(rows)
    under_library = sum(1 for r in rows if _norm(r["path"]).startswith(root + os.sep))
    log.info("The index has %d photos (%d under this library)", len(rows), under_library)
    by_sha = {r["sha"]: r for r in rows}
    by_path = {_norm(r["path"]): r for r in rows}  # if a path repeats, the newest row wins
    seen_shas: set[str] = set()
    walk_errors: list = []
    stats: Counter[str] = Counter()
    detail = log.debug if args.apply else log.info  # per-file lines: preview always, apply with -v
    hashed = 0
    pending = 0

    def write(sql: str, params: tuple) -> None:
        nonlocal pending
        if not conn:
            return
        try:
            conn.execute(sql, params)
            log.debug("Index %s", sql.split()[0])
            pending += 1
            if pending % 200 == 0:
                conn.commit()  # keep progress if the run is interrupted
                log.debug("Committed %d index changes so far", pending)
        except sqlite3.Error as error:
            stats["index_errors"] += 1
            if stats["index_errors"] == 1:
                log.warning("Could not write to the index %s: %s", db_path, error)

    log.info("Scanning %s ...", library)
    for path in iter_photos(library, onerror=walk_errors.append):
        stats["scanned"] += 1
        key = _norm(path)
        try:
            st = path.stat()
            size, mtime = st.st_size, st.st_mtime_ns
            row = by_path.get(key)
            if (
                row is not None
                and not args.rehash
                and row["mtime"] is not None
                and row["size"] == size
                and row["mtime"] == mtime
            ):
                log.debug("Unchanged (not re-hashed): %s", path)
                seen_shas.add(row["sha"])
                stats["unchanged"] += 1
                continue

            sha = sha256_of(path)
            hashed += 1
            log.debug("Hashed %s -> %s", path.name, sha[:12])
            seen_shas.add(sha)
            known = by_sha.get(sha)

            if known is None:
                taken, date_source, fields, read_error = read_photo_details(path)
                log.debug("Date for %s: %s (from %s)", path.name, taken.isoformat(timespec="seconds"), date_source)
                new_row = {"id": None, "sha": sha, "path": str(path), "size": size, "mtime": mtime}
                by_sha[sha] = new_row
                by_path[key] = new_row
                stats["added"] += 1
                stats[f"date:{date_source}"] += 1
                detail("ADD  %s  [%s]", path, date_source)
                write(
                    "INSERT OR IGNORE INTO photos (sha256, original_path, sorted_path, taken_at,"
                    " date_source, size_bytes, sorted_at, mtime_ns) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        sha, str(path), str(path), taken.isoformat(timespec="seconds"),
                        date_source, size, datetime.now().isoformat(timespec="seconds"), mtime,
                    ),
                )
                write(photo_meta.SAVE_EXIF_BY_SHA, (photo_meta.now(), *photo_meta.exif_values(fields), read_error, sha))
            elif _norm(known["path"]) == key:
                if known["size"] != size or known["mtime"] != mtime:
                    stats["timestamps_updated"] += 1
                    log.debug("Recording the current size and modified time for %s", path.name)
                    known.update(size=size, mtime=mtime)
                    write(
                        "UPDATE photos SET size_bytes=?, mtime_ns=? WHERE sha256=?",
                        (size, mtime, sha),
                    )
            elif os.path.exists(known["path"]):
                stats["duplicate_files"] += 1
                detail("DUPLICATE  %s  (same content as %s)", path, known["path"])
            else:
                stats["moved"] += 1
                detail("MOVED  %s  ->  %s", known["path"], path)
                known.update(path=str(path), size=size, mtime=mtime)
                by_path[key] = known
                write(
                    "UPDATE photos SET sorted_path=?, size_bytes=?, mtime_ns=? WHERE sha256=?",
                    (str(path), size, mtime, sha),
                )
        except OSError as error:
            stats["errors"] += 1
            log.error("%s: %s", path, error)

        if stats["scanned"] % PROGRESS_EVERY == 0:
            log.info("Progress: %d scanned, %d hashed", stats["scanned"], hashed)

    for error in walk_errors:
        stats["errors"] += 1
        log.error("Could not read folder: %s", error)
    log.info(
        "Scan complete: %d photos found, %d hashed, %d skipped as unchanged",
        stats["scanned"], hashed, stats["unchanged"],
    )

    # Camera details for photos indexed before phase 6 (each file is read once; later refreshes skip them).
    unread: list[dict] = []
    if not args.no_metadata:
        have = exif_ids(conn) if conn else exif_ids_read_only(db_path)
        unread = [
            r for r in by_sha.values()
            if r["id"] is not None and r["id"] not in have and r["sha"] in seen_shas
            and _norm(r["path"]).startswith(root + os.sep)
        ]
    stats["metadata_todo"] = len(unread)
    if unread and args.apply:
        log.info("Reading camera details for %d photos indexed before they were recorded ...", len(unread))
        for done, r in enumerate(unread, 1):
            _, fields, read_error = read_exif_details(Path(r["path"]))
            if read_error:
                stats["metadata_errors"] += 1
            write(photo_meta.SAVE_EXIF, (r["id"], photo_meta.now(), *photo_meta.exif_values(fields), read_error))
            stats["metadata_read"] += 1
            if done % PROGRESS_EVERY == 0:
                log.info("Progress: camera details read for %d of %d photos", done, len(unread))

    missing = [
        r for r in original_rows
        if r["sha"] not in seen_shas and _norm(r["path"]).startswith(root + os.sep)
    ]
    stats["missing"] = len(missing)
    for r in missing:
        detail("MISSING  %s", r["path"])

    # Decide whether the missing photos may be removed from the index.
    removed = 0
    removed_faces = 0
    blocked: str | None = None
    if missing and not args.keep_missing:
        if stats["errors"] or not stats["scanned"]:
            blocked = "the scan had errors or found no photos (is the drive connected?)"
        elif (
            len(missing) >= MASS_MISSING_MIN
            and len(missing) > MASS_MISSING_FRACTION * under_library
            and not args.force_prune
        ):
            blocked = (
                f"{len(missing)} of the {under_library} indexed photos in this library are missing (more than half), "
                "which looks like a disconnected or damaged folder rather than deliberate deletions. "
                "Check the library path; if you really did delete them, re-run with --force-prune"
            )
    removal_ids = [r["id"] for r in missing if r["id"] is not None]
    if missing and not args.keep_missing and not blocked:
        removed_faces = count_faces_for(db_path, removal_ids)
        if args.apply:
            log.info(
                "Removing %d photos that no longer exist from the index (and their %d face records)",
                len(missing), removed_faces,
            )
            for r in missing:
                write("DELETE FROM photos WHERE id = ?", (r["id"],))  # face rows go with it (ON DELETE CASCADE)
                removed += 1
    if blocked:
        log.warning("NOT removing missing photos from the index: %s.", blocked)

    if conn:
        conn.commit()
        conn.close()
        log.debug("Index saved and closed")

    log.info("--- Refresh summary ---")
    log.info("%-28s%d", "Photos scanned:", stats["scanned"])
    log.info("%-28s%d", "Unchanged (not re-hashed):", stats["unchanged"])
    log.info("%-28s%d", "Added to index:" if args.apply else "Would add:", stats["added"])
    log.info("%-28s%d", "Moved/renamed:" if args.apply else "Would update (moved):", stats["moved"])
    log.info("%-28s%d", "Timestamps refreshed:", stats["timestamps_updated"])
    log.info("%-28s%d (extra copies, left alone)", "Duplicate files:", stats["duplicate_files"])
    if args.apply:
        if stats["metadata_read"]:
            log.info("%-28s%d (%d could not be read)", "Camera details read:", stats["metadata_read"], stats["metadata_errors"])
    elif stats["metadata_todo"]:
        log.info("%-28s%d", "Camera details to read:", stats["metadata_todo"])
    log.info("%-28s%d", "Missing from disk:", stats["missing"])
    if missing and not args.keep_missing and not blocked:
        if args.apply:
            log.info("%-28s%d (and %d face records)", "Removed from the index:", removed, removed_faces)
        else:
            log.info("%-28s%d (and %d face records)", "Would remove from index:", len(missing), removed_faces)
    log.info("%-28s%d", "Errors:", stats["errors"])
    if stats["index_errors"]:
        log.warning("Index write failures: %d (see the warning above)", stats["index_errors"])
    for key in sorted(k for k in stats if k.startswith("date:")):
        log.info("  new photos dated by %s: %d", key[5:], stats[key])
    if stats["missing"] and args.keep_missing:
        log.info("  (--keep-missing: rows for missing files were left in the index)")
    log_index_location(db_path, args.apply)
    log.info("Finished in %.1f seconds", time.monotonic() - started)
    if not args.apply:
        log.info("Dry run only: the index was not changed. Re-run with --apply to update it.")
    return 1 if stats["errors"] or stats["index_errors"] else 0


# --------------------------------------------------------------------------
# metadata command: read camera details for photos already in the index
# --------------------------------------------------------------------------
def read_metadata(args: argparse.Namespace) -> int:
    """Read camera make/model, lens, exposure, size and GPS for indexed photos that don't have them yet."""
    started = time.monotonic()
    db_path = (args.db or default_db_path()).resolve()
    log.info("=== Camera details (%s) ===", "apply" if args.apply else "dry run")
    log_index_location(db_path, args.apply)
    if not db_path.exists():
        log.error("There is no index yet. Run 'sort ... --apply' or 'refresh ... --apply' first.")
        return 2
    try:
        conn = open_index(db_path) if args.apply else sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)
    except (OSError, sqlite3.Error) as error:
        log.error("Cannot open the index %s: %s", db_path, error)
        return 2
    stats: Counter[str] = Counter()
    fixed = 0
    try:
        have = set() if args.reread else exif_ids(conn)
        rows = [(pid, path) for pid, path in conn.execute("SELECT id, sorted_path FROM photos ORDER BY id") if pid not in have]
        total = len(rows)
        if args.limit is not None:
            rows = rows[: max(0, args.limit)]
        log.info("%d photos %s", total, "in the index" if args.reread else "have no camera details yet")
        if not args.apply:
            log.info("Would read %d photos. Re-run with --apply to read them.", len(rows))
            return 0
        try:
            for done, (photo_id, path_text) in enumerate(rows, 1):
                path = Path(path_text)
                if not path.is_file():
                    stats["missing"] += 1
                    log.debug("Not on disk any more: %s", path)
                    continue
                _, fields, read_error = read_exif_details(path)
                photo_meta.save_exif(conn, photo_id, fields, read_error)
                stats["read"] += 1
                if read_error:
                    stats["errors"] += 1
                    log.warning("Could not read %s: %s", path, read_error)
                elif fields and fields.get("latitude") is not None:
                    stats["gps"] += 1
                if fields and (fields.get("make") or fields.get("model")):
                    stats["camera"] += 1
                if done % PROGRESS_EVERY == 0:
                    conn.commit()
                    log.info("Progress: %d of %d photos", done, len(rows))
        except KeyboardInterrupt:
            conn.commit()
            log.warning("Stopped. %d photos were read and saved; run the same command again to continue.", stats["read"])
            return 130
        fixed = photo_meta.sync_search(conn)
        conn.commit()
    except sqlite3.Error as error:
        log.error("Could not read the index: %s", error)
        return 1
    finally:
        conn.close()
    log.info("--- Camera details summary ---")
    log.info("%-26s%d", "Photos read:", stats["read"])
    log.info("%-26s%d", "With camera make/model:", stats["camera"])
    log.info("%-26s%d", "With a GPS position:", stats["gps"])
    log.info("%-26s%d", "Could not be read:", stats["errors"])
    if stats["missing"]:
        log.info("%-26s%d (run 'refresh' to update the index)", "Not on disk any more:", stats["missing"])
    if fixed:
        log.info("Search index: %d entries brought up to date", fixed)
    log.info("Finished in %.1f seconds", time.monotonic() - started)
    return 1 if stats["errors"] else 0


# --------------------------------------------------------------------------
# info command
# --------------------------------------------------------------------------
def show_info(args: argparse.Namespace) -> int:
    db_path = (args.db or default_db_path()).resolve()
    log.info("Index database: %s", db_path)
    if is_network_path(db_path):
        log.warning("This path is on a network drive. SQLite can fail there; use a local path with --db.")
    if not db_path.exists():
        log.info("The file does not exist yet. It is created by the first 'sort ... --apply' or 'refresh ... --apply'.")
        return 0
    stat = db_path.stat()
    log.info(
        "Size: %s, last modified %s",
        human_size(stat.st_size),
        datetime.fromtimestamp(stat.st_mtime).isoformat(sep=" ", timespec="seconds"),
    )
    try:
        conn = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)
        try:
            total = conn.execute("SELECT COUNT(*) FROM photos").fetchone()[0]
            log.info("Photos indexed: %d", total)
            for date_source, count in conn.execute(
                "SELECT date_source, COUNT(*) FROM photos GROUP BY date_source ORDER BY 2 DESC"
            ):
                log.info("  dated by %s: %d", date_source, count)
            first, last = conn.execute("SELECT MIN(taken_at), MAX(taken_at) FROM photos").fetchone()
            if first:
                log.info("Photos taken from %s to %s", first[:10], last[:10])
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if {"face_scans", "faces"} <= tables:
                ok, failed = conn.execute(
                    "SELECT COALESCE(SUM(error IS NULL), 0), COALESCE(SUM(error IS NOT NULL), 0) FROM face_scans"
                ).fetchone()
                with_faces = conn.execute("SELECT COUNT(DISTINCT photo_id) FROM faces").fetchone()[0]
                face_total = conn.execute("SELECT COUNT(*) FROM faces").fetchone()[0]
                log.info(
                    "Face scan: %d of %d photos scanned (%d with faces, %d errors); %d faces stored",
                    ok, total, with_faces, failed, face_total,
                )
            if {"persons", "face_person"} <= tables:
                by_status = dict(conn.execute("SELECT status, COUNT(*) FROM face_person GROUP BY status"))
                people = conn.execute("SELECT COUNT(*) FROM persons").fetchone()[0]
                named = conn.execute("SELECT COUNT(*) FROM persons WHERE name IS NOT NULL AND name <> ''").fetchone()[0]
                if people or by_status:
                    log.info(
                        "People: %d groups (%d named); faces: %d auto, %d confirmed, %d to review, %d unassigned",
                        people, named, by_status.get("auto", 0), by_status.get("confirmed", 0),
                        by_status.get("review", 0), by_status.get("unassigned", 0),
                    )
                    if by_status.get("ignored"):
                        log.info("  %d faces were ignored in the UI", by_status["ignored"])
            if "photo_exif" in tables:
                read, cameras, located, failed = conn.execute(
                    "SELECT COUNT(*), COALESCE(SUM(make IS NOT NULL OR model IS NOT NULL), 0),"
                    " COALESCE(SUM(latitude IS NOT NULL), 0), COALESCE(SUM(error IS NOT NULL), 0) FROM photo_exif"
                ).fetchone()
                log.info(
                    "Camera details: %d of %d photos read (%d with camera, %d with GPS, %d unreadable)",
                    read, total, cameras, located, failed,
                )
            if "photo_annotations" in tables:
                kinds = dict(conn.execute("SELECT kind, COUNT(*) FROM photo_annotations GROUP BY kind"))
                if kinds:
                    log.info("Notes and tags: %s", ", ".join(f"{count} {kind}" for kind, count in sorted(kinds.items())))
            if "photo_embeddings" in tables:
                for model, count in conn.execute("SELECT model, COUNT(*) FROM photo_embeddings GROUP BY model ORDER BY model"):
                    log.info("Search vectors (%s): %d photos", model, count)
            log.info("Search index: %s", "full-text (FTS5)" if "photo_search" in tables else "not built yet (plain text match)")
        finally:
            conn.close()
    except sqlite3.Error as error:
        log.error("Could not read the index: %s", error)
        return 1
    return 0


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    base = argparse.ArgumentParser(add_help=False)
    base.add_argument("--db", type=Path, default=None, help="index database location (default: a local app-data folder; keep it off network drives)")
    base.add_argument("-v", "--verbose", action="store_true", help="detailed (debug) output")
    base.add_argument("--log-file", type=Path, default=None, help="also save a full debug log to this file")

    common = argparse.ArgumentParser(add_help=False, parents=[base])
    common.add_argument("--apply", action="store_true", help="make changes (default is a dry run)")

    parser = argparse.ArgumentParser(description="Photo sorter: sort photos by date and keep a local index.")
    commands = parser.add_subparsers(dest="command", required=True)

    sort_cmd = commands.add_parser(
        "sort", parents=[common],
        help="sort photos from SOURCE into Year/Month folders in DEST (default command)",
    )
    sort_cmd.add_argument("source", type=Path, help="folder containing the photos (searched recursively)")
    sort_cmd.add_argument("dest", type=Path, help="folder to sort photos into")
    sort_cmd.add_argument("--move", action="store_true", help="move instead of copy (originals deleted only after verification)")
    sort_cmd.add_argument(
        "--month-format",
        choices=["abbr", "full", "number", "name"],
        default="abbr",
        help="month folder style: abbr=03-mar (default), full=03-march, number=03, name=March",
    )

    refresh_cmd = commands.add_parser(
        "refresh", parents=[common],
        help="refresh the index from an already-sorted library",
    )
    refresh_cmd.add_argument("library", type=Path, help="the sorted library folder (Year/Month folders)")
    refresh_cmd.add_argument("--keep-missing", action="store_true", help="do not remove index rows for photos that no longer exist on disk (they are removed by default)")
    refresh_cmd.add_argument("--force-prune", action="store_true", help="allow removing missing photos even when more than half of the library is missing")
    refresh_cmd.add_argument("--prune", action="store_true", help=argparse.SUPPRESS)  # old name, now the default behaviour
    refresh_cmd.add_argument("--rehash", action="store_true", help="re-hash every file instead of trusting size + modified time")
    refresh_cmd.add_argument("--no-metadata", action="store_true", help="do not read camera details for photos indexed before they were recorded")

    faces_cmd = commands.add_parser(
        "faces", parents=[common],
        help="find faces in the indexed photos and store face embeddings (phase 2)",
    )
    faces_cmd.add_argument("--limit", type=int, default=None, help="only scan this many photos (good for a first test)")
    faces_cmd.add_argument("--rescan", action="store_true", help="scan every photo again, replacing earlier results")
    faces_cmd.add_argument("--retry-errors", action="store_true", help="also retry photos that failed before")
    faces_cmd.add_argument("--discard-confirmed", action="store_true", help="allow re-scanning photos whose faces you confirmed in the UI (their confirmations are erased)")
    hardware = faces_cmd.add_mutually_exclusive_group()
    hardware.add_argument("--gpu", action="store_true", help="require the NVIDIA GPU (CUDA): stop with an explanation instead of quietly using the CPU")
    hardware.add_argument("--cpu", action="store_true", help="ignore any GPU and use the CPU")
    faces_cmd.add_argument("--det-size", type=int, default=640, help="detector input size, multiple of 32; bigger finds smaller faces but is slower (default 640)")
    faces_cmd.add_argument("--min-score", type=float, default=0.5, help="minimum face detection confidence, 0-1 (default 0.5)")
    faces_cmd.add_argument("--max-side", type=int, default=1600, help="shrink photos so the longest side is at most this many pixels (default 1600)")
    faces_cmd.add_argument("--workers", type=int, default=3, help="background threads that load photos (default 3)")
    faces_cmd.add_argument("--model", default="buffalo_l", help="InsightFace model pack (default buffalo_l; buffalo_s is faster but less accurate)")
    faces_cmd.add_argument("--model-root", type=Path, default=None, help="where model files are kept (default ~/.insightface)")

    gpucheck_cmd = commands.add_parser(
        "gpucheck", parents=[base],
        help="check that the NVIDIA GPU can be used for face scanning, with a timing test",
    )
    gpucheck_cmd.add_argument("--model", default="buffalo_l", help="InsightFace model pack (default buffalo_l)")
    gpucheck_cmd.add_argument("--model-root", type=Path, default=None, help="where model files are kept (default ~/.insightface)")
    gpucheck_cmd.add_argument("--det-size", type=int, default=640, help="detector input size used for the test (default 640)")
    gpucheck_cmd.add_argument("--runs", type=int, default=5, help="timed runs per device (default 5)")

    cluster_cmd = commands.add_parser(
        "cluster", parents=[common],
        help="group similar faces into people and flag the uncertain ones (phase 3)",
    )
    cluster_cmd.add_argument("--recluster", action="store_true", help="start the AI's grouping over (people you named and faces you confirmed are always kept)")
    cluster_cmd.add_argument("--threshold", type=float, default=0.50, help="similarity needed to join a person automatically, 0-1 (default 0.50)")
    cluster_cmd.add_argument("--review-threshold", type=float, default=0.35, help="below this a face is not even suggested for a person (default 0.35)")
    cluster_cmd.add_argument("--merge-threshold", type=float, default=0.60, help="merge two AI-made groups whose averages are this similar (default 0.60)")
    cluster_cmd.add_argument("--margin", type=float, default=0.06, help="the best person must beat the runner-up by this much, else the face goes to review (default 0.06)")
    cluster_cmd.add_argument("--min-group-size", type=int, default=3, help="smaller new groups are dropped as one-off faces (default 3)")
    cluster_cmd.add_argument("--min-det-score", type=float, default=0.60, help="faces detected with less confidence are treated as low quality (default 0.60)")
    cluster_cmd.add_argument("--min-face-px", type=int, default=50, help="faces smaller than this many pixels are low quality (default 50)")
    cluster_cmd.add_argument("--min-blur", type=float, default=20.0, help="faces blurrier than this sharpness value are low quality (default 20)")
    cluster_cmd.add_argument("--max-yaw", type=float, default=45.0, help="faces turned more than this many degrees are low quality (default 45)")
    cluster_cmd.add_argument("--model", default=None, help="only use faces from this model pack (default: the one used most)")

    ui_cmd = commands.add_parser(
        "ui", parents=[base],
        help="open the web interface to name people and review uncertain faces (phase 4)",
    )
    ui_cmd.add_argument("--host", default="127.0.0.1", help="address to listen on (default 127.0.0.1: only this computer can connect)")
    ui_cmd.add_argument("--port", type=int, default=8765, help="port to listen on (default 8765)")
    ui_cmd.add_argument("--no-browser", action="store_true", help="do not open the browser automatically")

    metadata_cmd = commands.add_parser(
        "metadata", parents=[common],
        help="read camera details (make, model, lens, exposure, size, GPS) for photos in the index (phase 6)",
    )
    metadata_cmd.add_argument("--limit", type=int, default=None, help="only read this many photos")
    metadata_cmd.add_argument("--reread", action="store_true", help="read every photo again, not only the ones without details")

    commands.add_parser("info", parents=[base], help="show where the index database is and what it contains")
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] not in {"sort", "refresh", "info", "metadata", "faces", "gpucheck", "cluster", "ui", "-h", "--help"}:
        argv = ["sort", *argv]  # keep the original "sort_photos.py SOURCE DEST" form working
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose, args.log_file)
    if args.command == "refresh":
        return refresh(args)
    if args.command == "info":
        return show_info(args)
    if args.command == "metadata":
        return read_metadata(args)
    if args.command == "ui":
        try:
            import ui_server  # needs fastapi + uvicorn
        except ImportError as error:
            log.error("The web interface needs extra packages (%s). Install them with: pip install -r requirements-ui.txt", error)
            return 2
        return ui_server.run_ui(args)
    if args.command == "cluster":
        try:
            import face_cluster  # needs numpy only
        except ImportError as error:
            log.error("Grouping faces needs numpy (%s). Install it with: pip install -r requirements-faces.txt", error)
            return 2
        return face_cluster.cluster_faces(args)
    if args.command in ("faces", "gpucheck"):
        try:
            import face_scan  # needs extra packages, so only loaded for these commands
        except ImportError as error:
            log.error(
                "Face scanning needs extra packages (%s). Install them with: pip install -r requirements-faces.txt",
                error,
            )
            return 2
        return face_scan.gpu_check(args) if args.command == "gpucheck" else face_scan.scan_faces(args)
    return run(args)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):  # avoid crashes on odd filenames in the Windows console
        sys.stdout.reconfigure(errors="replace")
        sys.stderr.reconfigure(errors="replace")
    sys.exit(main())
