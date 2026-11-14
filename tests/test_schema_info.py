"""The index database: schema, upgrades, and the 'info' command."""
import sqlite3
from datetime import datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from types import SimpleNamespace

import pytest

import sort_photos as sp
from conftest import make_photo, scalar


def tables(db):
    conn = sqlite3.connect(db)
    try:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()


def test_open_index_creates_every_table(tmp_path):
    conn = sp.open_index(tmp_path / "x.db")
    conn.close()
    assert {"photos", "face_scans", "faces", "persons", "face_person", "face_rejections"} <= tables(tmp_path / "x.db")


def test_open_index_is_idempotent(tmp_path):
    for _ in range(3):
        sp.open_index(tmp_path / "x.db").close()
    assert scalar(tmp_path / "x.db", "SELECT COUNT(*) FROM photos") == 0


def test_persons_locked_column_is_added_to_an_older_index(tmp_path):
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE persons (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, created_at TEXT NOT NULL)")
    conn.execute("INSERT INTO persons (name, created_at) VALUES ('Ann', 'now')")
    conn.commit(); conn.close()
    sp.open_index(db).close()
    assert scalar(db, "SELECT locked FROM persons WHERE name = 'Ann'") == 0


def test_photos_mtime_column_is_added_to_an_older_index(tmp_path):
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE photos (id INTEGER PRIMARY KEY AUTOINCREMENT, sha256 TEXT NOT NULL UNIQUE, original_path TEXT,"
                 " sorted_path TEXT, taken_at TEXT, date_source TEXT, size_bytes INTEGER, sorted_at TEXT)")
    conn.commit(); conn.close()
    sp.open_index(db).close()
    conn = sqlite3.connect(db)
    assert "mtime_ns" in {r[1] for r in conn.execute("PRAGMA table_info(photos)")}
    conn.close()


def test_deleting_a_photo_cascades_to_faces_and_matches(tmp_path, face_index):
    index = face_index(3)
    photo = index.photo_of[index.faces[0][0]]
    conn = sp.open_index(index.db)
    pid = conn.execute("INSERT INTO persons (name, created_at) VALUES ('Ann','now')").lastrowid
    conn.execute("INSERT INTO face_person (face_id, person_id, similarity, status, reason, assigned_at) VALUES (?,?,?,?,?,?)",
                 (index.faces[0][0], pid, 1.0, "confirmed", "", "now"))
    conn.commit()
    conn.execute("DELETE FROM photos WHERE id = ?", (photo,))
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM faces").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM face_person").fetchone()[0] == 0
    conn.close()


def test_duplicate_hash_is_rejected_by_the_database(tmp_path):
    conn = sp.open_index(tmp_path / "x.db")
    row = ("same", "/o", "/s", "2020-01-01T00:00:00", "exif", 1, "now", 1)
    sql = "INSERT INTO photos (sha256, original_path, sorted_path, taken_at, date_source, size_bytes, sorted_at, mtime_ns) VALUES (?,?,?,?,?,?,?,?)"
    conn.execute(sql, row)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql, row)
    conn.close()


def test_default_db_path_uses_local_app_data(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "lad"))
    assert sp.default_db_path() == tmp_path / "lad" / "photo-sorter" / sp.DB_NAME


def test_default_db_path_falls_back_to_the_home_folder(tmp_path, monkeypatch):
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "me"))
    assert sp.default_db_path() == tmp_path / "me" / ".local" / "share" / "photo-sorter" / sp.DB_NAME


def test_network_path_detection(monkeypatch):
    # Swap in a stand-in "os" for sort_photos only. Changing the real os.name breaks pathlib
    # (on Windows, Path() refuses to build a PosixPath once os.name is "posix").
    monkeypatch.setattr(sp, "os", SimpleNamespace(name="nt"))
    assert sp.is_network_path(PureWindowsPath("\\\\server\\share\\photos")) is True
    assert sp.is_network_path(PureWindowsPath("\\\\?\\UNC\\server\\share")) is True
    monkeypatch.setattr(sp, "os", SimpleNamespace(name="posix"))
    assert sp.is_network_path(PurePosixPath("/mnt/photos")) is False


def test_human_size():
    assert sp.human_size(512) == "512 B"
    assert sp.human_size(2048).startswith("2.0")
    assert "MB" in sp.human_size(5 * 1024 * 1024)


# --------------------------------------------------------------------------- info
def test_info_before_any_index_exists(env, cli):
    r = cli("info")
    assert r.code == 0 and "does not exist yet" in r.out


def test_info_on_a_sorted_library(env, cli):
    make_photo(env.src / "a.jpg", 1, taken=datetime(2019, 3, 15))
    make_photo(env.src / "b.jpg", 2, mtime=datetime(2021, 6, 1))
    cli("sort", env.src, env.dest, "--apply")
    r = cli("info")
    assert r.code == 0 and "Photos indexed: 2" in r.out
    assert "dated by exif_original: 1" in r.out and "dated by file_date: 1" in r.out
    assert "from 2019-03-15 to 2021-06-01" in r.out


def test_info_shows_face_and_people_counts(env, cli, face_index):
    index = face_index(4, 3)
    r = cli("cluster", "--apply")
    r = cli("info")
    assert "Face scan: 7 of 7 photos scanned" in r.out and "People: 2 groups" in r.out


def test_info_on_a_corrupt_database_reports_an_error(env, cli):
    env.db.parent.mkdir(parents=True)
    env.db.write_bytes(b"this is not a sqlite file at all" * 20)
    r = cli("info")
    assert r.code == 1 and "Could not read the index" in r.err
