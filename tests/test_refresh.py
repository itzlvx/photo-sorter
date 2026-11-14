"""The 'refresh' command: bring the index in line with an already-sorted library on disk."""
import os
import shutil
from datetime import datetime

import pytest

import sort_photos as sp
from conftest import FaceIndex, make_photo, scalar, stat, tree


@pytest.fixture
def library(env):
    """A sorted library with three photos and no index yet."""
    for n, (month, name) in enumerate([(3, "a.jpg"), (4, "b.jpg"), (5, "c.jpg")], start=1):
        make_photo(env.dest / "2019" / f"{month:02d}-{sp.MONTH_ABBR[month - 1]}" / name, n, taken=datetime(2019, month, 10))
    return env.dest


def paths(db):
    import sqlite3
    conn = sqlite3.connect(db)
    try:
        return sorted(os.path.basename(r[0]) for r in conn.execute("SELECT sorted_path FROM photos"))
    finally:
        conn.close()


def test_dry_run_reports_but_writes_nothing(env, cli, library):
    r = cli("refresh", library)
    assert r.code == 0 and stat(r.out, "Would add:") == 3 and "Dry run only" in r.out
    assert not env.db.exists()


def test_apply_builds_the_index_from_the_library(env, cli, library):
    r = cli("refresh", library, "--apply")
    assert r.code == 0 and stat(r.out, "Added to index:") == 3
    assert paths(env.db) == ["a.jpg", "b.jpg", "c.jpg"]
    assert scalar(env.db, "SELECT date_source FROM photos WHERE sorted_path LIKE '%a.jpg'") == "exif_original"
    assert scalar(env.db, "SELECT substr(taken_at,1,10) FROM photos WHERE sorted_path LIKE '%a.jpg'") == "2019-03-10"


def test_second_refresh_does_not_rehash_unchanged_files(env, cli, library):
    cli("refresh", library, "--apply")
    r = cli("refresh", library, "--apply", "-v")
    assert stat(r.out, "Unchanged (not re-hashed):") == 3 and stat(r.out, "Added to index:") == 0
    assert "Hashed" not in r.out


def test_rehash_option_forces_hashing(env, cli, library):
    cli("refresh", library, "--apply")
    r = cli("refresh", library, "--apply", "--rehash", "-v")
    assert stat(r.out, "Unchanged (not re-hashed):") == 0 and "Hashed" in r.out


def test_new_files_are_added_later(env, cli, library):
    cli("refresh", library, "--apply")
    make_photo(library / "2020" / "01-jan" / "new.jpg", 50, taken=datetime(2020, 1, 5))
    r = cli("refresh", library, "--apply")
    assert stat(r.out, "Added to index:") == 1 and "new.jpg" in paths(env.db)


def test_renamed_or_moved_file_updates_the_path_and_keeps_the_row(env, cli, library):
    cli("refresh", library, "--apply")
    old_id = scalar(env.db, "SELECT id FROM photos WHERE sorted_path LIKE '%a.jpg'")
    target = library / "2019" / "03-mar" / "renamed.jpg"
    shutil.move(library / "2019" / "03-mar" / "a.jpg", target)
    r = cli("refresh", library, "--apply")
    assert stat(r.out, "Moved/renamed:") == 1 and stat(r.out, "Added to index:") == 0
    assert scalar(env.db, "SELECT id FROM photos WHERE sorted_path = ?", str(target)) == old_id
    assert scalar(env.db, "SELECT COUNT(*) FROM photos") == 3


def test_extra_copy_is_reported_and_left_alone(env, cli, library):
    cli("refresh", library, "--apply")
    shutil.copy(library / "2019" / "03-mar" / "a.jpg", library / "2019" / "03-mar" / "a copy.jpg")
    r = cli("refresh", library, "--apply")
    assert stat(r.out, "Duplicate files:") == 1 and scalar(env.db, "SELECT COUNT(*) FROM photos") == 3
    assert (library / "2019" / "03-mar" / "a copy.jpg").exists()


def test_deleted_files_are_removed_from_the_index_with_their_faces(env, cli, library):
    cli("refresh", library, "--apply")
    index = FaceIndex(env.db, library, files=False)
    photo = scalar(env.db, "SELECT id FROM photos WHERE sorted_path LIKE '%a.jpg'")
    index.add_face(0, photo_id=photo); index.commit(); index.close()
    assert scalar(env.db, "SELECT COUNT(*) FROM faces") == 1
    (library / "2019" / "03-mar" / "a.jpg").unlink()
    r = cli("refresh", library, "--apply")
    assert stat(r.out, "Removed from the index:") == 1
    assert paths(env.db) == ["b.jpg", "c.jpg"] and scalar(env.db, "SELECT COUNT(*) FROM faces") == 0


def test_dry_run_says_what_would_be_removed_but_keeps_it(env, cli, library):
    cli("refresh", library, "--apply")
    (library / "2019" / "03-mar" / "a.jpg").unlink()
    r = cli("refresh", library)
    assert stat(r.out, "Would remove from index:") == 1 and scalar(env.db, "SELECT COUNT(*) FROM photos") == 3


def test_keep_missing_leaves_rows_alone(env, cli, library):
    cli("refresh", library, "--apply")
    (library / "2019" / "03-mar" / "a.jpg").unlink()
    r = cli("refresh", library, "--apply", "--keep-missing")
    assert stat(r.out, "Missing from disk:") == 1 and scalar(env.db, "SELECT COUNT(*) FROM photos") == 3
    assert "--keep-missing" in r.out


def _thirty(env, cli):
    for n in range(30):
        make_photo(env.dest / "2019" / "03-mar" / f"p{n:02d}.jpg", n, taken=datetime(2019, 3, 1))
    cli("refresh", env.dest, "--apply")
    assert scalar(env.db, "SELECT COUNT(*) FROM photos") == 30


def test_a_few_deleted_photos_are_removed_normally(env, cli):
    _thirty(env, cli)
    for n in range(5):
        (env.dest / "2019" / "03-mar" / f"p{n:02d}.jpg").unlink()
    r = cli("refresh", env.dest, "--apply")
    assert stat(r.out, "Removed from the index:") == 5 and scalar(env.db, "SELECT COUNT(*) FROM photos") == 25


def test_more_than_half_missing_looks_like_a_disconnected_drive_and_is_blocked(env, cli):
    _thirty(env, cli)
    for n in range(20):
        (env.dest / "2019" / "03-mar" / f"p{n:02d}.jpg").unlink()
    r = cli("refresh", env.dest, "--apply")
    assert "NOT removing missing photos" in r.err and "--force-prune" in r.err
    assert scalar(env.db, "SELECT COUNT(*) FROM photos") == 30
    r = cli("refresh", env.dest, "--apply", "--force-prune")
    assert scalar(env.db, "SELECT COUNT(*) FROM photos") == 10


def test_an_empty_or_unreachable_library_never_wipes_the_index(env, cli, library):
    cli("refresh", library, "--apply")
    empty = env.tmp / "empty"
    empty.mkdir()
    cli("refresh", empty, "--apply")
    assert scalar(env.db, "SELECT COUNT(*) FROM photos") == 3          # rows outside this library are untouched


def test_files_deleted_when_the_scan_found_nothing_are_not_pruned(env, cli, library):
    cli("refresh", library, "--apply")
    for p in list(library.rglob("*.jpg")):
        p.unlink()
    r = cli("refresh", library, "--apply")
    assert "NOT removing" in r.err and scalar(env.db, "SELECT COUNT(*) FROM photos") == 3


def test_trash_folder_is_ignored(env, cli, library):
    make_photo(library / sp.TRASH_DIR / "old.jpg", 99, taken=datetime(2019, 1, 1))
    r = cli("refresh", library, "--apply")
    assert stat(r.out, "Photos scanned:") == 3 and "old.jpg" not in paths(env.db)


def test_missing_library_folder_is_an_error(env, cli):
    r = cli("refresh", env.tmp / "nope", "--apply")
    assert r.code == 2 and "Library folder not found" in r.err


def test_changed_file_content_at_same_path_is_added_as_new_and_old_is_missing(env, cli, library):
    cli("refresh", library, "--apply")
    target = library / "2019" / "03-mar" / "a.jpg"
    make_photo(target, 777, taken=datetime(2019, 3, 10))              # edited photo: new bytes, same name
    r = cli("refresh", library, "--apply")
    assert stat(r.out, "Added to index:") == 1 and stat(r.out, "Missing from disk:") == 1
    assert scalar(env.db, "SELECT COUNT(*) FROM photos") == 3


def test_refresh_after_sort_finds_nothing_new(env, cli):
    make_photo(env.src / "a.jpg", 1, taken=datetime(2019, 3, 15))
    cli("sort", env.src, env.dest, "--apply")
    r = cli("refresh", env.dest, "--apply")
    assert stat(r.out, "Added to index:") == 0 and stat(r.out, "Moved/renamed:") == 0 and stat(r.out, "Missing from disk:") == 0


def test_upgrade_from_an_old_index_without_mtime(env, cli, library):
    import sqlite3
    env.db.parent.mkdir(parents=True)
    conn = sqlite3.connect(env.db)
    conn.execute("CREATE TABLE photos (id INTEGER PRIMARY KEY AUTOINCREMENT, sha256 TEXT NOT NULL UNIQUE, original_path TEXT,"
                 " sorted_path TEXT, taken_at TEXT, date_source TEXT, size_bytes INTEGER, sorted_at TEXT)")
    conn.commit(); conn.close()
    r = cli("refresh", library, "--apply")
    assert r.code == 0 and "Upgrading the index" in r.out
    assert scalar(env.db, "SELECT COUNT(*) FROM photos") == 3
