"""The 'sort' command: copying/moving photos into Year/NN-mon folders and recording them in the index."""
import os
import sqlite3
from datetime import datetime

import pytest

import sort_photos as sp
from conftest import make_photo, scalar, stat, tree


def test_dry_run_changes_nothing(env, cli):
    make_photo(env.src / "a.jpg", 1, taken=datetime(2019, 3, 15, 9))
    r = cli("sort", env.src, env.dest)
    assert r.code == 0
    assert "Would sort:" in r.out and "Dry run only" in r.out
    assert not env.dest.exists() and not env.db.exists()
    assert tree(env.src) == ["a.jpg"]


def test_apply_sorts_into_year_and_month_folders(env, cli):
    make_photo(env.src / "a.jpg", 1, taken=datetime(2019, 3, 15, 9))
    make_photo(env.src / "trip" / "b.jpg", 2, taken=datetime(2021, 12, 31, 23))
    make_photo(env.src / "c.jpg", 3, taken=datetime(2019, 3, 1, 0))
    r = cli("sort", env.src, env.dest, "--apply")
    assert r.code == 0, r.text
    assert tree(env.dest) == ["2019/03-mar/a.jpg", "2019/03-mar/c.jpg", "2021/12-dec/b.jpg"]
    assert tree(env.src) == ["a.jpg", "c.jpg", "trip/b.jpg"]          # copy mode leaves the originals
    assert scalar(env.db, "SELECT COUNT(*) FROM photos") == 3
    assert scalar(env.db, "SELECT date_source FROM photos WHERE sorted_path LIKE '%b.jpg'") == "exif_original"


@pytest.mark.parametrize("style, folder", [("abbr", "03-mar"), ("full", "03-march"), ("number", "03"), ("name", "March")])
def test_month_format_option(env, cli, style, folder):
    make_photo(env.src / "a.jpg", 1, taken=datetime(2019, 3, 15))
    assert cli("sort", env.src, env.dest, "--apply", "--month-format", style).code == 0
    assert tree(env.dest) == [f"2019/{folder}/a.jpg"]


def test_photo_without_exif_is_sorted_by_file_date_and_reported(env, cli):
    make_photo(env.src / "a.jpg", 1, mtime=datetime(2014, 8, 9, 12))
    r = cli("sort", env.src, env.dest, "--apply")
    assert tree(env.dest) == ["2014/08-aug/a.jpg"]
    assert "dated by file_date: 1" in r.out and "no EXIF date" in r.out
    assert scalar(env.db, "SELECT date_source FROM photos") == "file_date"


def test_corrupt_image_is_still_filed_by_file_date(env, cli):
    bad = env.src / "broken.jpg"
    bad.write_bytes(b"not really a jpeg")
    os.utime(bad, (datetime(2010, 1, 2).timestamp(),) * 2)
    assert cli("sort", env.src, env.dest, "--apply").code == 0
    assert tree(env.dest) == ["2010/01-jan/broken.jpg"]


def test_duplicate_content_is_skipped_even_with_a_different_name(env, cli):
    a = make_photo(env.src / "a.jpg", 1, taken=datetime(2019, 3, 15))
    (env.src / "copy of a.jpg").write_bytes(a.read_bytes())
    r = cli("sort", env.src, env.dest, "--apply")
    assert stat(r.out, "Duplicates skipped:") == 1 and stat(r.out, "Photos sorted:") == 1
    assert len(tree(env.dest)) == 1 and scalar(env.db, "SELECT COUNT(*) FROM photos") == 1


def test_running_again_skips_everything_already_sorted(env, cli):
    make_photo(env.src / "a.jpg", 1, taken=datetime(2019, 3, 15))
    make_photo(env.src / "b.jpg", 2, taken=datetime(2019, 4, 15))
    cli("sort", env.src, env.dest, "--apply")
    r = cli("sort", env.src, env.dest, "--apply")
    assert r.code == 0 and stat(r.out, "Photos sorted:") == 0 and stat(r.out, "Duplicates skipped:") == 2
    assert len(tree(env.dest)) == 2


def test_same_name_different_content_gets_a_number(env, cli):
    make_photo(env.src / "one" / "IMG_1.jpg", 1, taken=datetime(2019, 3, 15))
    make_photo(env.src / "two" / "IMG_1.jpg", 2, taken=datetime(2019, 3, 20))
    cli("sort", env.src, env.dest, "--apply")
    assert tree(env.dest) == ["2019/03-mar/IMG_1.jpg", "2019/03-mar/IMG_1_1.jpg"]


def test_move_deletes_originals_after_copying(env, cli):
    make_photo(env.src / "a.jpg", 1, taken=datetime(2019, 3, 15))
    r = cli("sort", env.src, env.dest, "--apply", "--move")
    assert r.code == 0 and tree(env.src) == [] and tree(env.dest) == ["2019/03-mar/a.jpg"]


def test_move_dry_run_keeps_originals(env, cli):
    make_photo(env.src / "a.jpg", 1)
    cli("sort", env.src, env.dest, "--move")
    assert tree(env.src) == ["a.jpg"] and not env.dest.exists()


def test_transfer_removes_a_bad_copy_and_keeps_the_original(tmp_path, monkeypatch):
    src = make_photo(tmp_path / "a.jpg", 1)
    target = tmp_path / "out" / "a.jpg"
    monkeypatch.setattr(sp, "sha256_of", lambda path: "something else")
    with pytest.raises(OSError, match="verification"):
        sp.transfer(src, target, move=True, sha="expected")
    assert src.exists() and not target.exists()


def test_transfer_copy_mode_creates_folders_and_keeps_source(tmp_path):
    src = make_photo(tmp_path / "a.jpg", 1)
    target = tmp_path / "x" / "y" / "a.jpg"
    sp.transfer(src, target, move=False, sha=sp.sha256_of(src))
    assert src.exists() and target.read_bytes() == src.read_bytes()


def test_non_image_files_are_ignored_and_extensions_are_case_insensitive(env, cli):
    make_photo(env.src / "a.JPG", 1, taken=datetime(2019, 3, 15))
    make_photo(env.src / "b.png", 2, fmt="PNG", mtime=datetime(2019, 3, 15))
    (env.src / "notes.txt").write_text("hello")
    (env.src / "clip.mp4").write_bytes(b"\x00\x01")
    r = cli("sort", env.src, env.dest, "--apply")
    assert stat(r.out, "Photos scanned:") == 2
    assert sorted(p.split("/")[-1] for p in tree(env.dest)) == ["a.JPG", "b.png"]


def test_destination_inside_the_source_is_not_rescanned(env, cli):
    dest = env.src / "sorted"
    make_photo(env.src / "a.jpg", 1, taken=datetime(2019, 3, 15))
    cli("sort", env.src, dest, "--apply")
    r = cli("sort", env.src, dest, "--apply")
    assert stat(r.out, "Photos scanned:") == 1          # only a.jpg, not the sorted copy
    assert len(tree(dest)) == 1


def test_missing_source_folder_is_an_error(env, cli):
    r = cli("sort", env.tmp / "nope", env.dest)
    assert r.code == 2 and "Source folder not found" in r.err


def test_source_and_destination_must_differ(env, cli):
    r = cli("sort", env.src, env.src)
    assert r.code == 2 and "must be different" in r.err


def test_the_short_form_without_the_sort_word_still_works(env, cli):
    make_photo(env.src / "a.jpg", 1, taken=datetime(2019, 3, 15))
    r = cli(env.src, env.dest, "--apply")
    assert r.code == 0 and tree(env.dest) == ["2019/03-mar/a.jpg"]


def test_unusual_file_names(env, cli):
    make_photo(env.src / "Mom's 50th – café #3.jpg", 1, taken=datetime(2019, 3, 15))
    make_photo(env.src / "with space" / "ünï.jpg", 2, taken=datetime(2019, 3, 15))
    assert cli("sort", env.src, env.dest, "--apply").code == 0
    assert len(tree(env.dest)) == 2


def test_a_failed_copy_is_counted_as_an_error_and_the_rest_continue(env, cli, monkeypatch):
    make_photo(env.src / "a.jpg", 1, taken=datetime(2019, 3, 15))
    make_photo(env.src / "b.jpg", 2, taken=datetime(2019, 3, 16))
    real = sp.transfer

    def flaky(src, target, move, sha):
        if src.name == "a.jpg":
            raise OSError("disk full")
        real(src, target, move, sha)

    monkeypatch.setattr(sp, "transfer", flaky)
    r = cli("sort", env.src, env.dest, "--apply")
    assert r.code == 1 and "disk full" in r.err and stat(r.out, "Errors:") == 1
    assert tree(env.dest) == ["2019/03-mar/b.jpg"]


def test_index_write_failure_does_not_stop_sorting(env, cli, monkeypatch):
    make_photo(env.src / "a.jpg", 1, taken=datetime(2019, 3, 15))

    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(sp, "record_photo", broken)
    r = cli("sort", env.src, env.dest, "--apply")
    assert tree(env.dest) == ["2019/03-mar/a.jpg"]
    assert "Could not write to the index" in r.err


def test_an_unwritable_index_location_stops_before_copying(env, cli):
    blocker = env.tmp / "blocker"
    blocker.write_text("a file, not a folder")
    make_photo(env.src / "a.jpg", 1)
    r = cli("sort", env.src, env.dest, "--apply", db=blocker / "index.db")
    assert r.code == 2 and "Cannot open the index" in r.err and tree(env.dest) == [] and tree(env.src) == ["a.jpg"]


def test_log_file_gets_the_full_debug_log(env, cli):
    make_photo(env.src / "a.jpg", 1, taken=datetime(2019, 3, 15))
    log_file = env.tmp / "logs" / "run.log"
    cli("sort", env.src, env.dest, "--apply", "--log-file", log_file)
    text = log_file.read_text(encoding="utf-8")
    assert "Hashed a.jpg" in text and "Sort started" in text


def test_default_index_location_is_local_app_data(env, cli, tmp_path):
    make_photo(env.src / "a.jpg", 1, taken=datetime(2019, 3, 15))
    r = cli("sort", env.src, env.dest, "--apply", db=None)
    assert r.code == 0
    assert (tmp_path / "localappdata" / "photo-sorter" / sp.DB_NAME).exists()


def test_index_remembers_photos_across_runs_even_if_the_library_is_wiped(env, cli):
    make_photo(env.src / "a.jpg", 1, taken=datetime(2019, 3, 15))
    cli("sort", env.src, env.dest, "--apply")
    for p in env.dest.rglob("*.jpg"):
        p.unlink()
    r = cli("sort", env.src, env.dest, "--apply")
    assert stat(r.out, "Duplicates skipped:") == 1      # the index says it was already sorted
