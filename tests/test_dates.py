"""How a photo's date is worked out (EXIF first, then the file's own date) and how month folders are named."""
from datetime import datetime

import pytest

import sort_photos as sp
from conftest import make_photo


@pytest.mark.parametrize("value, expected", [
    ("2019:03:15 10:20:30", datetime(2019, 3, 15, 10, 20, 30)),
    ("2019-03-15 10:20:30", datetime(2019, 3, 15, 10, 20, 30)),
    (b"2019:03:15 10:20:30\x00", datetime(2019, 3, 15, 10, 20, 30)),
    ("  2019:03:15 10:20:30  ", datetime(2019, 3, 15, 10, 20, 30)),
    ("2019:03:15 10:20:30.123", datetime(2019, 3, 15, 10, 20, 30)),
    ("", None), (None, None), ("not a date", None),
    ("0000:00:00 00:00:00", None),          # cameras with an unset clock
    ("1899:12:31 23:59:59", None),          # before 1900 is treated as bogus
    ("2019:13:45 10:20:30", None),          # impossible month
])
def test_parse_exif_date(value, expected):
    assert sp.parse_exif_date(value) == expected


def test_exif_original_beats_digitized_beats_modified(tmp_path):
    p = make_photo(tmp_path / "a.jpg", taken="2015:01:01 08:00:00", digitized="2016:02:02 08:00:00", modified="2017:03:03 08:00:00")
    assert sp.read_exif_date(p) == (datetime(2015, 1, 1, 8), "exif_original")
    p = make_photo(tmp_path / "b.jpg", 1, digitized="2016:02:02 08:00:00", modified="2017:03:03 08:00:00")
    assert sp.read_exif_date(p) == (datetime(2016, 2, 2, 8), "exif_digitized")
    p = make_photo(tmp_path / "c.jpg", 2, modified="2017:03:03 08:00:00")
    assert sp.read_exif_date(p) == (datetime(2017, 3, 3, 8), "exif_modified")


def test_bogus_original_falls_through_to_next_exif_date(tmp_path):
    p = make_photo(tmp_path / "a.jpg", taken="0000:00:00 00:00:00", digitized="2016:02:02 08:00:00")
    assert sp.read_exif_date(p) == (datetime(2016, 2, 2, 8), "exif_digitized")


def test_no_exif_uses_the_file_date(tmp_path):
    p = make_photo(tmp_path / "a.jpg", mtime=datetime(2012, 7, 4, 12, 0))
    assert sp.read_exif_date(p) is None
    taken, source = sp.get_taken_date(p)
    assert source == "file_date" and (taken.year, taken.month) == (2012, 7)


def test_png_without_exif_uses_the_file_date(tmp_path):
    p = make_photo(tmp_path / "a.png", fmt="PNG", mtime=datetime(2011, 5, 6, 12, 0))
    taken, source = sp.get_taken_date(p)
    assert source == "file_date" and taken.month == 5


def test_a_corrupt_image_does_not_crash_date_reading(tmp_path):
    p = tmp_path / "broken.jpg"
    p.write_bytes(b"this is not a jpeg")
    assert sp.read_exif_date(p) is None
    assert sp.get_taken_date(p)[1] == "file_date"


@pytest.mark.parametrize("style, month, expected", [
    ("abbr", 1, "01-jan"), ("abbr", 12, "12-dec"),
    ("full", 3, "03-march"), ("number", 7, "07"), ("name", 9, "September"),
])
def test_month_folder_styles(style, month, expected):
    assert sp.month_folder(month, style) == expected


def test_default_month_style_is_abbr():
    assert sp.month_folder(2, "anything-else") == "02-feb"


def test_choose_destination_free_name(tmp_path):
    target, dup = sp.choose_destination(tmp_path, "a.jpg", "sha", set())
    assert target == tmp_path / "a.jpg" and dup is False


def test_choose_destination_same_content_is_a_duplicate(tmp_path):
    existing = make_photo(tmp_path / "a.jpg", 5)
    target, dup = sp.choose_destination(tmp_path, "a.jpg", sp.sha256_of(existing), set())
    assert target == existing and dup is True


def test_choose_destination_clash_gets_a_number_and_skips_reserved(tmp_path):
    make_photo(tmp_path / "a.jpg", 5)
    target, dup = sp.choose_destination(tmp_path, "a.jpg", "different", set())
    assert target == tmp_path / "a_1.jpg" and dup is False
    target, _ = sp.choose_destination(tmp_path, "a.jpg", "different", {tmp_path / "a_1.jpg"})
    assert target == tmp_path / "a_2.jpg"


def test_sha256_is_content_based(tmp_path):
    a, b = make_photo(tmp_path / "a.jpg", 1), make_photo(tmp_path / "b.jpg", 2)
    c = tmp_path / "c.jpg"
    c.write_bytes(a.read_bytes())
    assert sp.sha256_of(a) == sp.sha256_of(c) != sp.sha256_of(b)


def test_iter_photos_filters_by_extension_and_skips_excluded_and_trash(tmp_path):
    make_photo(tmp_path / "a.jpg", 1); make_photo(tmp_path / "sub" / "b.PNG", 2, fmt="PNG")
    (tmp_path / "notes.txt").write_text("x"); (tmp_path / "movie.mp4").write_bytes(b"x")
    make_photo(tmp_path / "skipme" / "c.jpg", 3)
    make_photo(tmp_path / sp.TRASH_DIR / "d.jpg", 4)
    names = {p.name for p in sp.iter_photos(tmp_path, exclude=(tmp_path / "skipme").resolve())}
    assert names == {"a.jpg", "b.PNG"}
