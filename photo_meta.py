"""Phase 6: photo metadata that is not about faces.

Tables (all keyed by photo id, and removed together with the photo, like faces):

  photo_exif          camera make/model, lens, exposure, pixel size and GPS position, read from the file once.
                      A row exists for every photo whose file was read (even with no EXIF), so it is not re-read.
  photo_embeddings    one vector per photo per model (for "search by description" in phase 7).
  photo_annotations   text about a photo: kind is caption, tag, ocr or note; source is 'you' for what you
                      typed, or 'model:<name>' for what a program wrote. Re-running a model replaces only its
                      own rows, never yours.
  photo_search        an SQLite full-text index (FTS5) over file names/folders and annotation text. It is kept
                      up to date by triggers inside the database, so every program that writes the index keeps it
                      right. If this SQLite has no FTS5, search falls back to a slower plain text match.

New features add rows (a new model name, a new kind), not columns or tables.

This module only needs Pillow (numpy is imported only by the embedding helpers).
"""
from __future__ import annotations

import logging
import re
import sqlite3
from datetime import datetime

log = logging.getLogger("photo_sorter")

YOU = "you"                       # source of everything typed by a person in the web interface
KINDS = {"caption", "tag", "ocr", "note"}
MAX_NOTE = 4000
EXIF_IFD = 0x8769
GPS_IFD = 0x8825
TAG_MAKE, TAG_MODEL, TAG_ORIENTATION = 0x010F, 0x0110, 0x0112
TAG_LENS_MAKE, TAG_LENS_MODEL = 0xA433, 0xA434
TAG_EXPOSURE, TAG_FNUMBER, TAG_ISO, TAG_FOCAL = 0x829A, 0x829D, 0x8827, 0x920A
EXIF_COLUMNS = ("make", "model", "lens", "width", "height", "orientation", "f_number", "exposure_s",
                "iso", "focal_mm", "latitude", "longitude", "altitude_m")

META_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS photo_exif (
        photo_id    INTEGER PRIMARY KEY REFERENCES photos(id) ON DELETE CASCADE,
        read_at     TEXT NOT NULL,
        make        TEXT,
        model       TEXT,
        lens        TEXT,
        width       INTEGER,          -- pixel size of the upright photo (after EXIF rotation)
        height      INTEGER,
        orientation INTEGER,
        f_number    REAL,
        exposure_s  REAL,
        iso         INTEGER,
        focal_mm    REAL,
        latitude    REAL,             -- decimal degrees, south and west are negative
        longitude   REAL,
        altitude_m  REAL,
        error       TEXT              -- set when the file could not be read
    )""",
    """CREATE TABLE IF NOT EXISTS photo_embeddings (
        photo_id   INTEGER NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
        model      TEXT NOT NULL,
        dim        INTEGER NOT NULL,
        vector     BLOB NOT NULL,     -- dim float32 (4 bytes each) or float16 (2 bytes each) values
        created_at TEXT NOT NULL,
        PRIMARY KEY (photo_id, model)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_photo_embeddings_model ON photo_embeddings(model)",
    """CREATE TABLE IF NOT EXISTS photo_annotations (
        id         INTEGER PRIMARY KEY,
        photo_id   INTEGER NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
        kind       TEXT NOT NULL,
        text       TEXT NOT NULL,
        score      REAL,
        source     TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_photo_annotations_photo ON photo_annotations(photo_id)",
    "CREATE INDEX IF NOT EXISTS idx_photo_annotations_source ON photo_annotations(source, kind)",
]

SEARCH_TABLE = "CREATE VIRTUAL TABLE photo_search USING fts5(name, notes, tokenize = 'unicode61 remove_diacritics 2')"
NOTES_OF = "COALESCE((SELECT group_concat(text, ' ') FROM photo_annotations WHERE photo_id = {0}), '')"
SEARCH_TRIGGERS = [
    """CREATE TRIGGER IF NOT EXISTS photo_search_add AFTER INSERT ON photos BEGIN
        INSERT OR REPLACE INTO photo_search (rowid, name, notes) VALUES (new.id, COALESCE(new.sorted_path, ''), '');
    END""",
    """CREATE TRIGGER IF NOT EXISTS photo_search_move AFTER UPDATE OF sorted_path ON photos BEGIN
        UPDATE photo_search SET name = COALESCE(new.sorted_path, '') WHERE rowid = new.id;
    END""",
    """CREATE TRIGGER IF NOT EXISTS photo_search_drop AFTER DELETE ON photos BEGIN
        DELETE FROM photo_search WHERE rowid = old.id;
    END""",
    f"""CREATE TRIGGER IF NOT EXISTS photo_search_note_add AFTER INSERT ON photo_annotations BEGIN
        UPDATE photo_search SET notes = {NOTES_OF.format("new.photo_id")} WHERE rowid = new.photo_id;
    END""",
    f"""CREATE TRIGGER IF NOT EXISTS photo_search_note_change AFTER UPDATE ON photo_annotations BEGIN
        UPDATE photo_search SET notes = {NOTES_OF.format("old.photo_id")} WHERE rowid = old.photo_id;
        UPDATE photo_search SET notes = {NOTES_OF.format("new.photo_id")} WHERE rowid = new.photo_id;
    END""",
    f"""CREATE TRIGGER IF NOT EXISTS photo_search_note_drop AFTER DELETE ON photo_annotations BEGIN
        UPDATE photo_search SET notes = {NOTES_OF.format("old.photo_id")} WHERE rowid = old.photo_id;
    END""",
]


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------
def fts_available(conn: sqlite3.Connection) -> bool:
    """True when this SQLite was built with FTS5 (the Python builds on Windows and macOS are)."""
    try:
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS temp.fts_probe USING fts5(x)")
        conn.execute("DROP TABLE temp.fts_probe")
        return True
    except sqlite3.Error:
        return False


def has_search(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'photo_search'").fetchone() is not None


def ensure_meta_schema(conn: sqlite3.Connection) -> None:
    """Create the phase 6 tables (safe to call every time the index is opened)."""
    for statement in META_SCHEMA:
        conn.execute(statement)
    if has_search(conn):
        for statement in SEARCH_TRIGGERS:
            conn.execute(statement)
    elif fts_available(conn):
        conn.execute(SEARCH_TABLE)
        for statement in SEARCH_TRIGGERS:
            conn.execute(statement)
        added = sync_search(conn)
        if added:
            log.info("Added a search index for the file names and notes of %d photos", added)
    else:
        log.debug("This SQLite has no FTS5; searching notes uses a plain text match")


def sync_search(conn: sqlite3.Connection) -> int:
    """Make the search index match the photos table (adds missing rows, drops stale ones). Returns rows fixed."""
    if not has_search(conn):
        return 0
    stale = conn.execute("DELETE FROM photo_search WHERE rowid NOT IN (SELECT id FROM photos)").rowcount
    added = conn.execute(
        "INSERT INTO photo_search (rowid, name, notes) SELECT p.id, COALESCE(p.sorted_path, ''), "
        + NOTES_OF.format("p.id") + " FROM photos p WHERE p.id NOT IN (SELECT rowid FROM photo_search)"
    ).rowcount
    return max(stale, 0) + max(added, 0)


# --------------------------------------------------------------------------
# Reading camera details from an open Pillow image
# --------------------------------------------------------------------------
def _text(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="ignore")
    text = re.sub(r"\s+", " ", str(value).replace("\x00", "")).strip()
    return text[:120] or None


def _number(value) -> float | None:
    if isinstance(value, (tuple, list)):
        value = value[0] if value else None
    try:
        number = float(value)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    return number if number == number and abs(number) != float("inf") else None   # drop NaN / inf


def _degrees(value, ref) -> float | None:
    """GPS (degrees, minutes, seconds) plus N/S/E/W -> signed decimal degrees."""
    try:
        parts = [float(v) for v in value]
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    if not parts or any(p != p for p in parts):
        return None
    while len(parts) < 3:
        parts.append(0.0)
    degrees = parts[0] + parts[1] / 60 + parts[2] / 3600
    if (_text(ref) or "").upper()[:1] in ("S", "W"):
        degrees = -degrees
    return round(degrees, 7)


def exif_fields(image) -> dict:
    """Camera details from an open Pillow image. Missing values are None; never raises for bad EXIF."""
    fields: dict = dict.fromkeys(EXIF_COLUMNS)
    try:
        width, height = image.size
    except Exception:
        width = height = None
    try:
        exif = image.getexif()
    except Exception as error:
        log.debug("Unreadable EXIF block: %s", error)
        exif = {}
    def ifd(tag):
        try:
            return exif.get_ifd(tag) if exif else {}
        except Exception:
            return {}
    sub, gps = ifd(EXIF_IFD), ifd(GPS_IFD)
    orientation = _number(exif.get(TAG_ORIENTATION)) if exif else None
    fields["orientation"] = int(orientation) if orientation and 1 <= orientation <= 8 else None
    if fields["orientation"] in (5, 6, 7, 8) and width and height:
        width, height = height, width                     # stored as the photo is shown, upright
    fields["width"], fields["height"] = width, height
    fields["make"] = _text(exif.get(TAG_MAKE)) if exif else None
    model = _text(exif.get(TAG_MODEL)) if exif else None
    if model and fields["make"] and model.lower().startswith(fields["make"].lower()):
        model = model[len(fields["make"]):].strip() or model       # "Canon" + "Canon EOS 5D" -> "EOS 5D"
    fields["model"] = model
    lens = _text(sub.get(TAG_LENS_MODEL))
    lens_make = _text(sub.get(TAG_LENS_MAKE))
    fields["lens"] = f"{lens_make} {lens}" if lens and lens_make and not lens.lower().startswith(lens_make.lower()) else lens
    fields["f_number"] = _number(sub.get(TAG_FNUMBER)) or None
    fields["exposure_s"] = _number(sub.get(TAG_EXPOSURE)) or None
    iso = _number(sub.get(TAG_ISO))
    fields["iso"] = int(iso) if iso and iso > 0 else None
    fields["focal_mm"] = _number(sub.get(TAG_FOCAL)) or None
    if gps:
        lat, lon = _degrees(gps.get(2), gps.get(1)), _degrees(gps.get(4), gps.get(3))
        if lat is not None and lon is not None and -90 <= lat <= 90 and -180 <= lon <= 180 and (lat, lon) != (0.0, 0.0):
            fields["latitude"], fields["longitude"] = lat, lon
            altitude = _number(gps.get(6))
            if altitude is not None:
                below = gps.get(5)
                if isinstance(below, bytes):
                    below = below[:1] == b"\x01"
                fields["altitude_m"] = -altitude if below in (True, 1) else altitude
    return fields


SAVE_EXIF = (
    "INSERT OR REPLACE INTO photo_exif (photo_id, read_at, " + ", ".join(EXIF_COLUMNS) + ", error) "
    "VALUES (?, ?, " + ", ".join("?" * len(EXIF_COLUMNS)) + ", ?)"
)
SAVE_EXIF_BY_SHA = (
    "INSERT OR REPLACE INTO photo_exif (photo_id, read_at, " + ", ".join(EXIF_COLUMNS) + ", error) "
    "SELECT id, ?, " + ", ".join("?" * len(EXIF_COLUMNS)) + ", ? FROM photos WHERE sha256 = ?"
)


def exif_values(fields: dict | None) -> list:
    fields = fields or {}
    return [fields.get(column) for column in EXIF_COLUMNS]


def save_exif(conn: sqlite3.Connection, photo_id: int, fields: dict | None, error: str | None = None) -> None:
    conn.execute(SAVE_EXIF, (photo_id, now(), *exif_values(fields), error))


def camera_json(row) -> dict | None:
    """The photo_exif row as the web interface shows it (None when the file was never read)."""
    if row is None:
        return None
    data = {column: row[column] for column in EXIF_COLUMNS}
    data["error"] = row["error"]
    camera = " ".join(part for part in (data["make"], data["model"]) if part)
    data["camera"] = camera or None
    settings = []
    if data["f_number"]:
        settings.append(f"f/{data['f_number']:g}")
    if data["exposure_s"]:
        exposure = data["exposure_s"]
        settings.append(f"1/{round(1 / exposure)} s" if exposure < 0.5 else f"{exposure:g} s")
    if data["iso"]:
        settings.append(f"ISO {data['iso']}")
    if data["focal_mm"]:
        settings.append(f"{data['focal_mm']:g} mm")
    data["settings"] = " · ".join(settings) or None
    return data


# --------------------------------------------------------------------------
# Annotations: notes, tags, captions, OCR text
# --------------------------------------------------------------------------
def _check(kind: str, text: str) -> str:
    if kind not in KINDS:
        raise ValueError(f"Unknown annotation kind {kind!r} (expected one of {sorted(KINDS)})")
    text = (text or "").strip()
    if not text:
        raise ValueError("An annotation needs some text.")
    return text


def add_annotation(conn: sqlite3.Connection, photo_id: int, kind: str, text: str, source: str,
                   score: float | None = None) -> int:
    text = _check(kind, text)
    if not source:
        raise ValueError("Say who wrote the annotation (source).")
    return conn.execute(
        "INSERT INTO photo_annotations (photo_id, kind, text, score, source, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (photo_id, kind, text, score, source, now()),
    ).lastrowid


def replace_model_annotations(conn: sqlite3.Connection, photo_id: int, source: str, kind: str,
                              items: list[tuple[str, float | None]]) -> int:
    """Replace what one program wrote (this source and kind) for a photo. Your own rows are never touched."""
    if source == YOU or not source:
        raise ValueError("Programs must use their own source name, such as 'model:clip'.")
    if kind not in KINDS:
        raise ValueError(f"Unknown annotation kind {kind!r}")
    conn.execute("DELETE FROM photo_annotations WHERE photo_id = ? AND source = ? AND kind = ?", (photo_id, source, kind))
    count = 0
    for text, score in items:
        if (text or "").strip():
            add_annotation(conn, photo_id, kind, text, source, score)
            count += 1
    return count


def get_note(conn: sqlite3.Connection, photo_id: int) -> str:
    row = conn.execute(
        "SELECT text FROM photo_annotations WHERE photo_id = ? AND kind = 'note' AND source = ? ORDER BY id LIMIT 1",
        (photo_id, YOU),
    ).fetchone()
    return row[0] if row else ""


def clean_note(text: str | None) -> str:
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", text)                 # keep line breaks and tabs
    text = "\n".join(line.rstrip() for line in text.split("\n")).strip()
    return text


def set_note(conn: sqlite3.Connection, photo_id: int, text: str | None) -> str:
    """Your note on a photo: one per photo. Empty text removes it. Returns the saved text."""
    text = clean_note(text)
    if len(text) > MAX_NOTE:
        raise ValueError(f"That note is too long (the limit is {MAX_NOTE} characters).")
    existing = conn.execute(
        "SELECT id FROM photo_annotations WHERE photo_id = ? AND kind = 'note' AND source = ? ORDER BY id", (photo_id, YOU)
    ).fetchall()
    if not text:
        conn.execute("DELETE FROM photo_annotations WHERE photo_id = ? AND kind = 'note' AND source = ?", (photo_id, YOU))
        return ""
    if existing:
        conn.execute("UPDATE photo_annotations SET text = ?, created_at = ? WHERE id = ?", (text, now(), existing[0][0]))
        for (extra,) in existing[1:]:
            conn.execute("DELETE FROM photo_annotations WHERE id = ?", (extra,))
    else:
        add_annotation(conn, photo_id, "note", text, YOU)
    return text


def annotations_for(conn: sqlite3.Connection, photo_id: int) -> list[dict]:
    return [
        {"id": r[0], "kind": r[1], "text": r[2], "score": r[3], "source": r[4], "created_at": r[5]}
        for r in conn.execute(
            "SELECT id, kind, text, score, source, created_at FROM photo_annotations WHERE photo_id = ?"
            " ORDER BY kind, (source = ?) DESC, COALESCE(score, 0) DESC, id", (photo_id, YOU),
        )
    ]


# --------------------------------------------------------------------------
# Embeddings (one vector per photo per model)
# --------------------------------------------------------------------------
def save_embedding(conn: sqlite3.Connection, photo_id: int, model: str, vector, half: bool = False) -> None:
    """Store a vector; half=True keeps it as 16-bit numbers (half the space, plenty for search)."""
    import numpy as np
    array = np.asarray(vector, dtype=np.float16 if half else np.float32).ravel()
    if not model or array.size == 0:
        raise ValueError("An embedding needs a model name and at least one number.")
    if not np.all(np.isfinite(array)):
        raise ValueError("The embedding contains invalid numbers.")
    conn.execute(
        "INSERT OR REPLACE INTO photo_embeddings (photo_id, model, dim, vector, created_at) VALUES (?, ?, ?, ?, ?)",
        (photo_id, model, int(array.size), array.tobytes(), now()),
    )


def load_embeddings(conn: sqlite3.Connection, model: str):
    """(photo ids, float32 matrix with one row per photo) for one model. Rows of the wrong size are skipped."""
    import numpy as np
    ids, rows = [], []
    for photo_id, dim, blob in conn.execute(
        "SELECT photo_id, dim, vector FROM photo_embeddings WHERE model = ? ORDER BY photo_id", (model,)
    ):
        if dim and len(blob) == dim * 4:
            rows.append(np.frombuffer(blob, dtype=np.float32))
        elif dim and len(blob) == dim * 2:
            rows.append(np.frombuffer(blob, dtype=np.float16).astype(np.float32))
        else:
            log.debug("Skipping a damaged %s vector for photo %s", model, photo_id)
            continue
        ids.append(photo_id)
    if rows and len({r.size for r in rows}) > 1:
        raise ValueError(f"The stored {model} vectors have different sizes; re-index that model.")
    matrix = np.stack(rows) if rows else np.zeros((0, 0), dtype=np.float32)
    return ids, matrix


# --------------------------------------------------------------------------
# Search
# --------------------------------------------------------------------------
def fts_phrase(word: str) -> str | None:
    """One typed word as an FTS5 prefix phrase ('lake-pla' -> '"lake pla"*'), or None if it has no letters/digits."""
    tokens = re.findall(r"\w+", word, flags=re.UNICODE)
    if not tokens:
        return None
    return '"' + " ".join(tokens).replace('"', '""') + '"*'


def like_pattern(word: str) -> str:
    return "%" + word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def word_condition(word: str, use_fts: bool, alias: str = "ph") -> tuple[str, list]:
    """SQL that is true when a photo matches one typed word: in its file/folder name, or in its notes and tags."""
    pattern = like_pattern(word)
    parts = [f"{alias}.sorted_path LIKE ? ESCAPE '\\' COLLATE NOCASE"]
    params: list = [pattern]
    phrase = fts_phrase(word) if use_fts else None
    if phrase:
        parts.append(f"{alias}.id IN (SELECT rowid FROM photo_search WHERE photo_search MATCH ?)")
        params.append("notes : " + phrase)
    else:
        parts.append(f"EXISTS (SELECT 1 FROM photo_annotations a WHERE a.photo_id = {alias}.id"
                     " AND a.text LIKE ? ESCAPE '\\' COLLATE NOCASE)")
        params.append(pattern)
    return "(" + " OR ".join(parts) + ")", params
