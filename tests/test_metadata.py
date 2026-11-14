"""Phase 6: metadata beyond faces. Camera details (EXIF), notes and tags, embeddings, and the full-text search index."""
import sqlite3
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest
from PIL import Image
from PIL.TiffImagePlugin import IFDRational as R

import photo_meta as pm
import sort_photos as sp
from conftest import connect, make_photo, scalar, stat


def camera_photo(path: Path, seed=1, size=(80, 60), make="Canon", model="Canon EOS 5D", lens="EF 50mm f/1.8",
                 orientation=None, gps=((40, 30, 36), "N", (73, 59, 0), "W", 12.0), taken=datetime(2021, 7, 4, 15),
                 f_number=R(28, 10), exposure=R(1, 125), iso=200, focal=R(50, 1)) -> Path:
    """A small JPEG with camera details, exposure settings and (optionally) a GPS position."""
    path.parent.mkdir(parents=True, exist_ok=True)
    pixels = np.random.default_rng(seed).integers(0, 255, (size[1], size[0], 3), dtype=np.uint8)
    exif = Image.Exif()
    if make:
        exif[pm.TAG_MAKE] = make
    if model:
        exif[pm.TAG_MODEL] = model
    if orientation:
        exif[pm.TAG_ORIENTATION] = orientation
    sub = exif.get_ifd(sp.EXIF_IFD)
    if taken:
        sub[sp.TAG_DATETIME_ORIGINAL] = taken.strftime("%Y:%m:%d %H:%M:%S")
    if lens:
        sub[pm.TAG_LENS_MODEL] = lens
    if f_number:
        sub[pm.TAG_FNUMBER] = f_number
    if exposure:
        sub[pm.TAG_EXPOSURE] = exposure
    if iso:
        sub[pm.TAG_ISO] = iso
    if focal:
        sub[pm.TAG_FOCAL] = focal
    if gps:
        lat, lat_ref, lon, lon_ref, alt = gps
        g = exif.get_ifd(pm.GPS_IFD)
        g[1], g[2] = lat_ref, tuple(R(v) for v in lat)
        g[3], g[4] = lon_ref, tuple(R(v) for v in lon)
        if alt is not None:
            g[5], g[6] = b"\x00", R(int(alt * 10), 10)
    Image.fromarray(pixels).save(path, "JPEG", exif=exif)
    return path


def fields_of(path: Path) -> dict:
    with Image.open(path) as img:
        return pm.exif_fields(img)


def exif_row(db: Path, photo_id: int):
    conn = connect(db)
    try:
        return conn.execute("SELECT * FROM photo_exif WHERE photo_id = ?", (photo_id,)).fetchone()
    finally:
        conn.close()


def search_ids(conn, word: str) -> set[int]:
    sql, params = pm.word_condition(word, pm.has_search(conn))
    return {r[0] for r in conn.execute(f"SELECT ph.id FROM photos ph WHERE {sql}", params)}


@pytest.fixture
def db(tmp_path):
    """An open index with three photos in it."""
    conn = sp.open_index(tmp_path / "x.db")
    conn.row_factory = sqlite3.Row
    for n, path in enumerate(["/lib/2020/01-jan/beach.jpg", "/lib/2020/02-feb/IMG_0002.jpg", "/lib/2021/03-mar/cake.png"], 1):
        conn.execute("INSERT INTO photos (sha256, original_path, sorted_path, taken_at, date_source, size_bytes, sorted_at)"
                     " VALUES (?, ?, ?, '2020-01-01T00:00:00', 'file_date', 1, 'now')", (f"s{n}", path, path))
    conn.commit()
    yield conn
    conn.close()


# --------------------------------------------------------------------------- schema
def test_phase6_tables_and_search_index_are_created(tmp_path):
    sp.open_index(tmp_path / "x.db").close()
    names = {r[0] for r in sqlite3.connect(tmp_path / "x.db").execute("SELECT name FROM sqlite_master")}
    assert {"photo_exif", "photo_embeddings", "photo_annotations", "photo_search"} <= names
    assert {"photo_search_add", "photo_search_move", "photo_search_drop", "photo_search_note_add",
            "photo_search_note_change", "photo_search_note_drop"} <= names


def test_opening_twice_is_harmless(tmp_path):
    for _ in range(3):
        sp.open_index(tmp_path / "x.db").close()
    assert scalar(tmp_path / "x.db", "SELECT COUNT(*) FROM photo_search") == 0


def test_an_older_index_gets_a_search_index_with_its_existing_photos(tmp_path):
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.execute(sp.SCHEMA)
    for n in range(3):
        conn.execute("INSERT INTO photos (sha256, original_path, sorted_path, taken_at, date_source, size_bytes, sorted_at)"
                     " VALUES (?, 'x', ?, 't', 'file_date', 1, 'now')", (f"s{n}", f"/lib/old_{n}.jpg"))
    conn.commit(); conn.close()
    conn = sp.open_index(db)
    assert conn.execute("SELECT COUNT(*) FROM photo_search").fetchone()[0] == 3
    assert search_ids(conn, "old_1") == {2}
    conn.close()


def test_deleting_a_photo_removes_its_metadata_and_search_entry(db):
    pm.save_exif(db, 1, {"make": "Canon"})
    pm.set_note(db, 1, "Grandma's 80th")
    pm.save_embedding(db, 1, "clip", [1.0, 0.0])
    db.commit()
    db.execute("DELETE FROM photos WHERE id = 1")
    db.commit()
    for table in ("photo_exif", "photo_annotations", "photo_embeddings"):
        assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table
    assert db.execute("SELECT COUNT(*) FROM photo_search WHERE rowid = 1").fetchone()[0] == 0
    assert search_ids(db, "grandma") == set()


def test_sync_search_repairs_missing_and_stale_rows(db):
    db.execute("DELETE FROM photo_search WHERE rowid = 2")
    db.execute("INSERT INTO photo_search (rowid, name, notes) VALUES (99, 'ghost', '')")
    assert pm.sync_search(db) == 2
    assert {r[0] for r in db.execute("SELECT rowid FROM photo_search")} == {1, 2, 3}
    assert pm.sync_search(db) == 0


# --------------------------------------------------------------------------- reading camera details
def test_camera_details_are_read(tmp_path):
    f = fields_of(camera_photo(tmp_path / "a.jpg"))
    assert f["make"] == "Canon" and f["model"] == "EOS 5D"          # the make is not repeated in the model
    assert f["lens"] == "EF 50mm f/1.8"
    assert (f["width"], f["height"], f["orientation"]) == (80, 60, None)
    assert f["f_number"] == pytest.approx(2.8) and f["exposure_s"] == pytest.approx(1 / 125)
    assert f["iso"] == 200 and f["focal_mm"] == pytest.approx(50)
    assert f["latitude"] == pytest.approx(40.51) and f["longitude"] == pytest.approx(-73.983333, abs=1e-5)
    assert f["altitude_m"] == pytest.approx(12.0)


def test_south_and_west_are_negative_and_east_positive(tmp_path):
    f = fields_of(camera_photo(tmp_path / "a.jpg", gps=((33, 52, 0), "S", (151, 12, 0), "E", None)))
    assert f["latitude"] == pytest.approx(-33.866667, abs=1e-5) and f["longitude"] == pytest.approx(151.2)
    assert f["altitude_m"] is None


def test_a_zero_gps_position_is_treated_as_missing(tmp_path):
    f = fields_of(camera_photo(tmp_path / "a.jpg", gps=((0, 0, 0), "N", (0, 0, 0), "E", 0.0)))
    assert f["latitude"] is None and f["longitude"] is None


def test_rotated_photos_report_their_upright_size(tmp_path):
    f = fields_of(camera_photo(tmp_path / "a.jpg", size=(80, 60), orientation=6))
    assert (f["width"], f["height"], f["orientation"]) == (60, 80, 6)


def test_a_photo_without_exif_still_has_a_size(tmp_path):
    f = fields_of(make_photo(tmp_path / "a.png", 1, size=(30, 20), fmt="PNG"))
    assert (f["width"], f["height"]) == (30, 20)
    assert all(f[k] is None for k in ("make", "model", "lens", "latitude", "iso"))


def test_bad_values_do_not_break_reading(tmp_path):
    f = fields_of(camera_photo(tmp_path / "a.jpg", make="  Can\x00on  ", model="", lens=None, f_number=R(0, 0), iso=0, gps=None))
    assert f["make"] == "Canon" and f["model"] is None and f["f_number"] is None and f["iso"] is None


def test_read_exif_details_reports_unreadable_files(tmp_path):
    bad = tmp_path / "bad.jpg"
    bad.write_bytes(b"not a photo")
    found, fields, error = sp.read_exif_details(bad)
    assert found is None and fields is None and error


def test_the_date_and_camera_come_from_one_read(tmp_path):
    taken, source, fields, error = sp.read_photo_details(camera_photo(tmp_path / "a.jpg"))
    assert (taken, source) == (datetime(2021, 7, 4, 15), "exif_original") and fields["make"] == "Canon" and error is None


def test_camera_json_formats_settings():
    row = dict.fromkeys(pm.EXIF_COLUMNS) | {"make": "Canon", "model": "EOS 5D", "f_number": 2.8, "exposure_s": 0.008,
                                            "iso": 200, "focal_mm": 50.0, "error": None}
    data = pm.camera_json(row)
    assert data["camera"] == "Canon EOS 5D" and data["settings"] == "f/2.8 · 1/125 s · ISO 200 · 50 mm"
    assert pm.camera_json(dict(row, exposure_s=2.0, f_number=None, iso=None, focal_mm=None))["settings"] == "2 s"
    assert pm.camera_json(None) is None


# --------------------------------------------------------------------------- sort, refresh, metadata, info
def test_sort_records_camera_details(env, cli):
    camera_photo(env.src / "a.jpg")
    make_photo(env.src / "plain.png", 2, fmt="PNG")
    assert cli("sort", env.src, env.dest, "--apply").code == 0
    assert scalar(env.db, "SELECT COUNT(*) FROM photo_exif") == 2
    row = exif_row(env.db, scalar(env.db, "SELECT id FROM photos WHERE sorted_path LIKE '%a.jpg'"))
    assert row["make"] == "Canon" and row["latitude"] == pytest.approx(40.51) and row["error"] is None


def test_sort_dry_run_writes_nothing(env, cli):
    camera_photo(env.src / "a.jpg")
    assert cli("sort", env.src, env.dest).code == 0 and not env.db.exists()


def test_refresh_records_camera_details_for_new_photos(env, cli):
    camera_photo(env.dest / "2021" / "07-jul" / "a.jpg")
    r = cli("refresh", env.dest, "--apply")
    assert r.code == 0 and scalar(env.db, "SELECT make FROM photo_exif") == "Canon"
    assert "Camera details read:" not in r.out           # new photos are read with their date, not in a second pass


def test_refresh_fills_in_camera_details_for_photos_indexed_earlier(env, cli):
    camera_photo(env.dest / "2021" / "07-jul" / "a.jpg")
    make_photo(env.dest / "2021" / "07-jul" / "b.jpg", 2)
    assert cli("refresh", env.dest, "--apply").code == 0
    conn = connect(env.db)
    conn.execute("DELETE FROM photo_exif")                # as if indexed by an older version
    conn.commit(); conn.close()
    preview = cli("refresh", env.dest)
    assert stat(preview.out, "Camera details to read:") == 2 and scalar(env.db, "SELECT COUNT(*) FROM photo_exif") == 0
    r = cli("refresh", env.dest, "--apply")
    assert stat(r.out, "Camera details read:") == 2 and scalar(env.db, "SELECT COUNT(*) FROM photo_exif") == 2
    again = cli("refresh", env.dest, "--apply")
    assert "Camera details read:" not in again.out       # each photo is read only once


def test_refresh_no_metadata_skips_the_catch_up(env, cli):
    camera_photo(env.dest / "2021" / "07-jul" / "a.jpg")
    cli("refresh", env.dest, "--apply")
    conn = connect(env.db)
    conn.execute("DELETE FROM photo_exif")
    conn.commit(); conn.close()
    r = cli("refresh", env.dest, "--apply", "--no-metadata")
    assert r.code == 0 and scalar(env.db, "SELECT COUNT(*) FROM photo_exif") == 0


def test_refresh_only_reads_photos_in_the_library_being_refreshed(env, cli):
    camera_photo(env.dest / "2021" / "07-jul" / "a.jpg")
    other = env.tmp / "other"
    camera_photo(other / "2020" / "01-jan" / "b.jpg", seed=5)
    cli("refresh", env.dest, "--apply"); cli("refresh", other, "--apply")
    conn = connect(env.db)
    conn.execute("DELETE FROM photo_exif")
    conn.commit(); conn.close()
    r = cli("refresh", env.dest, "--apply")
    assert stat(r.out, "Camera details read:") == 1


def test_refresh_keeps_notes_when_a_photo_is_moved(env, cli):
    old = camera_photo(env.dest / "2021" / "07-jul" / "a.jpg")
    cli("refresh", env.dest, "--apply")
    conn = sp.open_index(env.db)
    pm.set_note(conn, 1, "Lake Placid trip")
    conn.commit(); conn.close()
    new = env.dest / "2021" / "08-aug" / "renamed.jpg"
    new.parent.mkdir(parents=True)
    old.rename(new)
    assert stat(cli("refresh", env.dest, "--apply").out, "Moved/renamed:") == 1
    conn = sp.open_index(env.db)
    assert pm.get_note(conn, 1) == "Lake Placid trip"
    assert search_ids(conn, "renamed") == {1} and search_ids(conn, "placid") == {1}
    conn.close()


def test_metadata_command_preview_apply_and_limit(env, cli):
    for n in range(3):
        camera_photo(env.dest / "2021" / "07-jul" / f"p{n}.jpg", seed=n)
    cli("refresh", env.dest, "--apply")
    conn = connect(env.db)
    conn.execute("DELETE FROM photo_exif")
    conn.commit(); conn.close()
    preview = cli("metadata")
    assert preview.code == 0 and "3 photos have no camera details yet" in preview.out and "Would read 3" in preview.out
    assert scalar(env.db, "SELECT COUNT(*) FROM photo_exif") == 0
    r = cli("metadata", "--apply", "--limit", "2")
    assert r.code == 0 and stat(r.out, "Photos read:") == 2 and stat(r.out, "With a GPS position:") == 2
    r = cli("metadata", "--apply")
    assert stat(r.out, "Photos read:") == 1
    assert stat(cli("metadata", "--apply").out, "Photos read:") == 0
    assert stat(cli("metadata", "--apply", "--reread").out, "Photos read:") == 3


def test_metadata_command_skips_missing_files_and_records_unreadable_ones(env, cli):
    camera_photo(env.dest / "2021" / "07-jul" / "good.jpg")
    make_photo(env.dest / "2021" / "07-jul" / "gone.jpg", 2)
    bad = make_photo(env.dest / "2021" / "07-jul" / "bad.jpg", 3)
    cli("refresh", env.dest, "--apply")
    conn = connect(env.db)
    conn.execute("DELETE FROM photo_exif")
    conn.commit(); conn.close()
    (env.dest / "2021" / "07-jul" / "gone.jpg").unlink()
    bad.write_bytes(b"broken")
    r = cli("metadata", "--apply")
    assert r.code == 1 and stat(r.out, "Could not be read:") == 1 and stat(r.out, "Not on disk any more:") == 1
    assert scalar(env.db, "SELECT COUNT(*) FROM photo_exif WHERE error IS NOT NULL") == 1
    assert scalar(env.db, "SELECT COUNT(*) FROM photo_exif") == 2       # the missing file is retried after a refresh


def test_metadata_command_needs_an_index(env, cli):
    r = cli("metadata", "--apply")
    assert r.code == 2 and "no index yet" in r.err


def test_metadata_command_can_be_stopped_and_resumed(env, cli, monkeypatch):
    for n in range(3):
        camera_photo(env.dest / "2021" / "07-jul" / f"p{n}.jpg", seed=n)
    cli("refresh", env.dest, "--apply")
    conn = connect(env.db)
    conn.execute("DELETE FROM photo_exif")
    conn.commit(); conn.close()
    real, calls = sp.read_exif_details, []

    def stop_on_third(path):
        calls.append(path)
        if len(calls) == 3:
            raise KeyboardInterrupt
        return real(path)

    monkeypatch.setattr(sp, "read_exif_details", stop_on_third)
    r = cli("metadata", "--apply")
    assert r.code == 130 and "run the same command again" in r.err
    assert scalar(env.db, "SELECT COUNT(*) FROM photo_exif") == 2
    monkeypatch.setattr(sp, "read_exif_details", real)
    assert stat(cli("metadata", "--apply").out, "Photos read:") == 1


def test_info_reports_metadata(env, cli):
    camera_photo(env.src / "a.jpg")
    cli("sort", env.src, env.dest, "--apply")
    conn = sp.open_index(env.db)
    pm.set_note(conn, 1, "hello")
    pm.add_annotation(conn, 1, "tag", "beach", "model:test", 0.9)
    pm.save_embedding(conn, 1, "clip-b32", [0.1, 0.2])
    conn.commit(); conn.close()
    out = cli("info").out
    assert "Camera details: 1 of 1 photos read (1 with camera, 1 with GPS, 0 unreadable)" in out
    assert "Notes and tags: 1 note, 1 tag" in out and "Search vectors (clip-b32): 1 photos" in out
    assert "Search index: full-text (FTS5)" in out


# --------------------------------------------------------------------------- annotations
def test_one_note_per_photo_replace_and_remove(db):
    assert pm.set_note(db, 1, "  first\r\nline two \x07 ") == "first\nline two"
    assert pm.set_note(db, 1, "second") == "second"
    assert db.execute("SELECT COUNT(*) FROM photo_annotations WHERE photo_id = 1").fetchone()[0] == 1
    assert pm.get_note(db, 1) == "second"
    assert pm.set_note(db, 1, "   ") == "" and pm.get_note(db, 1) == ""


def test_a_note_that_is_too_long_is_refused(db):
    with pytest.raises(ValueError):
        pm.set_note(db, 1, "x" * (pm.MAX_NOTE + 1))


def test_model_annotations_never_touch_your_own(db):
    pm.set_note(db, 1, "mine")
    pm.add_annotation(db, 1, "tag", "my tag", pm.YOU)
    assert pm.replace_model_annotations(db, 1, "model:clip", "tag", [("dog", 0.9), ("beach", 0.7), ("", 0.1)]) == 2
    assert pm.replace_model_annotations(db, 1, "model:clip", "tag", [("cat", 0.8)]) == 1   # a re-run replaces its own rows
    rows = {(a["source"], a["text"]) for a in pm.annotations_for(db, 1)}
    assert rows == {(pm.YOU, "mine"), (pm.YOU, "my tag"), ("model:clip", "cat")}
    with pytest.raises(ValueError):
        pm.replace_model_annotations(db, 1, pm.YOU, "tag", [])


@pytest.mark.parametrize("kind, text, source", [("weird", "x", "you"), ("tag", "  ", "you"), ("tag", "x", "")])
def test_bad_annotations_are_refused(db, kind, text, source):
    with pytest.raises(ValueError):
        pm.add_annotation(db, 1, kind, text, source)


# --------------------------------------------------------------------------- embeddings
def test_embeddings_round_trip_in_32_and_16_bit(db):
    pm.save_embedding(db, 1, "clip", [0.5, -0.25, 1.0])
    pm.save_embedding(db, 2, "clip", np.array([0.1, 0.2, 0.3]), half=True)
    pm.save_embedding(db, 3, "other", [1.0, 0.0])
    ids, matrix = pm.load_embeddings(db, "clip")
    assert ids == [1, 2] and matrix.dtype == np.float32 and matrix.shape == (2, 3)
    assert np.allclose(matrix[0], [0.5, -0.25, 1.0]) and np.allclose(matrix[1], [0.1, 0.2, 0.3], atol=1e-3)
    assert db.execute("SELECT length(vector) FROM photo_embeddings WHERE photo_id = 2").fetchone()[0] == 6


def test_saving_an_embedding_again_replaces_it(db):
    pm.save_embedding(db, 1, "clip", [1.0, 0.0])
    pm.save_embedding(db, 1, "clip", [0.0, 1.0])
    ids, matrix = pm.load_embeddings(db, "clip")
    assert ids == [1] and np.allclose(matrix[0], [0.0, 1.0])


def test_bad_embeddings_are_refused_or_skipped(db):
    for bad in ([], [float("nan")]):
        with pytest.raises(ValueError):
            pm.save_embedding(db, 1, "clip", bad)
    db.execute("INSERT INTO photo_embeddings VALUES (1, 'clip', 3, x'0102', 'now')")      # damaged row
    assert pm.load_embeddings(db, "clip")[0] == []
    assert pm.load_embeddings(db, "nothing")[1].shape == (0, 0)


def test_mixed_vector_sizes_for_one_model_are_reported(db):
    pm.save_embedding(db, 1, "clip", [1.0, 0.0])
    pm.save_embedding(db, 2, "clip", [1.0, 0.0, 0.0])
    with pytest.raises(ValueError, match="re-index"):
        pm.load_embeddings(db, "clip")


# --------------------------------------------------------------------------- search
def test_search_finds_file_names_folders_and_notes(db):
    pm.set_note(db, 2, "Grandma's 80th birthday, Lake Placid")
    pm.replace_model_annotations(db, 3, "model:clip", "tag", [("chocolate cake", 0.8)])
    db.commit()
    assert search_ids(db, "beach") == {1}
    assert search_ids(db, "2020") == {1, 2}                    # folder names
    assert search_ids(db, "0002") == {2}                      # part of a word in a file name still matches
    assert search_ids(db, "grandma") == {2} and search_ids(db, "plac") == {2}    # note words, by prefix
    assert search_ids(db, "lake-placid") == {2}
    assert search_ids(db, "chocolate") == {3}
    assert search_ids(db, "nothing") == set()


def test_search_handles_punctuation_and_quotes(db):
    pm.set_note(db, 1, 'He said "cheese"')
    db.commit()
    assert search_ids(db, '"cheese"') == {1}
    assert search_ids(db, '"') == {1}                       # no letters: a plain text match, and the note has a quote
    assert search_ids(db, "%") == set() and search_ids(db, "*") == set()


def test_search_ignores_accents(db):
    pm.set_note(db, 1, "Café in Montréal")
    db.commit()
    assert search_ids(db, "montreal") == {1} and search_ids(db, "café") == {1}


def test_search_follows_note_edits_and_renames(db):
    pm.set_note(db, 1, "old words")
    db.commit()
    pm.set_note(db, 1, "new words")
    db.execute("UPDATE photos SET sorted_path = '/lib/2020/01-jan/sunset.jpg' WHERE id = 1")
    db.commit()
    assert search_ids(db, "old") == set() and search_ids(db, "new") == {1}
    assert search_ids(db, "sunset") == {1} and search_ids(db, "beach") == set()
    db.execute("UPDATE photo_annotations SET photo_id = 2")                 # moving an annotation updates both photos
    assert search_ids(db, "new") == {2}


def test_search_without_fts5_falls_back_to_a_text_match(tmp_path, monkeypatch):
    monkeypatch.setattr(pm, "fts_available", lambda conn: False)
    conn = sp.open_index(tmp_path / "x.db")
    assert not pm.has_search(conn)
    conn.execute("INSERT INTO photos (sha256, original_path, sorted_path, taken_at, date_source, size_bytes, sorted_at)"
                 " VALUES ('s', 'x', '/lib/a.jpg', 't', 'file_date', 1, 'now')")
    pm.set_note(conn, 1, "100% Lake Placid")
    conn.commit()
    assert search_ids(conn, "placid") == {1} and search_ids(conn, "100%") == {1} and search_ids(conn, "a.jpg") == {1}
    assert search_ids(conn, "zzz") == set()
    conn.close()


def test_fts_phrase():
    assert pm.fts_phrase("lake-placid") == '"lake placid"*'
    assert pm.fts_phrase("%%") is None


# --------------------------------------------------------------------------- web interface
@pytest.fixture
def site(ui):
    """The synthetic web interface, with camera details for the first photo."""
    pytest.importorskip("fastapi")
    ui.first = ui.j("/api/photos?limit=1")["photos"][0]["id"]
    conn = sp.open_index(ui.db)
    pm.save_exif(conn, ui.first, {"make": "Canon", "model": "EOS 5D", "f_number": 2.8, "width": 96, "height": 72,
                                  "latitude": 40.51, "longitude": -73.98})
    conn.commit(); conn.close()
    return ui


def test_photo_detail_includes_camera_details_and_note(site):
    data = site.j(f"/api/photos/{site.first}")
    assert data["camera"]["camera"] == "Canon EOS 5D" and data["camera"]["settings"] == "f/2.8"
    assert data["camera"]["latitude"] == 40.51 and data["note"] == "" and data["annotations"] == []
    other = site.j("/api/photos?limit=2")["photos"][1]["id"]
    assert site.j(f"/api/photos/{other}")["camera"] is None


def test_save_change_and_clear_a_note(site):
    pid = site.first
    assert site.ok(site.post(f"/api/photos/{pid}/note", text="  Grandma's 80th  "))["note"] == "Grandma's 80th"
    assert site.j(f"/api/photos/{pid}")["note"] == "Grandma's 80th"
    site.ok(site.post(f"/api/photos/{pid}/note", text="Lake Placid"))
    assert site.j(f"/api/photos/{pid}")["note"] == "Lake Placid"
    assert site.ok(site.post(f"/api/photos/{pid}/note", text=""))["note"] == ""
    assert scalar(site.db, "SELECT COUNT(*) FROM photo_annotations") == 0


def test_note_errors(site):
    assert site.post("/api/photos/999999/note", text="x").status_code == 404
    assert site.post(f"/api/photos/{site.first}/note", text="x" * (pm.MAX_NOTE + 1)).status_code == 400
    assert site.client.post(f"/api/photos/{site.first}/note", json={"text": "x"}).status_code == 403   # header required


def test_photos_page_search_and_filter_use_notes(site):
    site.ok(site.post(f"/api/photos/{site.first}/note", text="Birthday cake at the lake"))
    found = site.j("/api/photos?q=birthday")
    assert found["total"] == 1 and found["photos"][0]["id"] == site.first
    assert site.j("/api/photos?q=lake%20birth")["total"] == 1                  # every word must match
    assert site.j("/api/photos?q=birthday%20zzz")["total"] == 0
    assert site.j("/api/photos?q=img0001")["total"] == 1                       # file names still work
    assert site.j("/api/library")["counts"]["notes"] == 1
    assert [p["id"] for p in site.j("/api/photos?filter=notes")["photos"]] == [site.first]


def test_model_tags_show_in_the_detail_but_not_as_your_note(site):
    conn = sp.open_index(site.db)
    pm.replace_model_annotations(conn, site.first, "model:clip", "tag", [("dog", 0.9)])
    conn.commit(); conn.close()
    data = site.j(f"/api/photos/{site.first}")
    assert data["note"] == "" and [(a["kind"], a["text"]) for a in data["annotations"]] == [("tag", "dog")]
    assert site.j("/api/photos?q=dog")["total"] == 1


def test_deleting_a_photo_in_the_interface_removes_its_note(site):
    site.ok(site.post(f"/api/photos/{site.first}/note", text="remember me"))
    site.ok(site.post(f"/api/photos/{site.first}/delete"))
    assert scalar(site.db, "SELECT COUNT(*) FROM photo_annotations") == 0
    assert site.j("/api/photos?q=remember")["total"] == 0


def test_renaming_a_photo_updates_search(site):
    site.ok(site.post(f"/api/photos/{site.first}/rename", name="lighthouse"))
    assert site.j("/api/photos?q=lighthouse")["total"] == 1
