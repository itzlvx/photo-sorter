"""Phase 5: browsing the whole library, quick filters, thumbnails, and rename / delete (single and several)."""
import io
import os
import time
from pathlib import Path

import pytest
from PIL import Image

import sort_photos as sp
from conftest import make_photo, scalar, tree

pytest.importorskip("fastapi")


@pytest.fixture
def named(ui):
    """The three groups, named Ann / Bob / Cy (biggest first), plus a scanned photo with no faces and an unscanned one."""
    for person, name in zip(ui.people(), ["Ann", "Bob", "Cy"]):
        ui.ok(ui.post(f"/api/people/{person['id']}/name", name=name))
    ui.noface = ui.index.add_photo(month=5, year=2020, scanned=0)
    ui.unscanned = ui.index.add_photo(month=6, year=2020, scanned=None)
    ui.index.commit()
    ui.ids = {p["name"]: p["id"] for p in ui.people()}
    return ui


def photos(ui, **params):
    query = "&".join(f"{k}={v}" for k, v in params.items())
    return ui.j(f"/api/photos?limit=300&{query}")


# --------------------------------------------------------------------------- listing and filters
def test_library_overview(named):
    lib = named.j("/api/library")
    assert lib["total"] == 78 and sum(y["count"] for y in lib["years"]) == 78
    assert [y["year"] for y in lib["years"]] == sorted((y["year"] for y in lib["years"]), reverse=True)
    assert set(lib["counts"]) == {"unlabeled", "review", "noface", "unscanned"}
    assert lib["counts"]["noface"] == 1 and lib["counts"]["unscanned"] == 1


def test_everything_is_listed_newest_first_and_oldest_first(named):
    data = photos(named)
    assert data["total"] == 78 and len(data["photos"]) == 78
    taken = [p["taken"] for p in data["photos"]]
    assert taken == sorted(taken, reverse=True)
    assert [p["taken"] for p in photos(named, order="old")["photos"]] == sorted(taken)


def test_paging(named):
    first = named.j("/api/photos?limit=10&offset=0")["photos"]
    rest = named.j("/api/photos?limit=10&offset=75")["photos"]
    assert len(first) == 10 and len(rest) == 3 and not {p["id"] for p in first} & {p["id"] for p in rest}
    assert len(named.j("/api/photos?limit=100000")["photos"]) <= 300


def test_people_chips_lists_named_people_with_photo_counts(named):
    chips = named.j("/api/people/chips")
    assert [c["name"] for c in chips["people"]] == ["Ann", "Bob", "Cy"] and chips["unnamed_groups"] == 0
    assert chips["people"][0]["photos"] >= chips["people"][1]["photos"] >= chips["people"][2]["photos"]
    assert named.get(f"/api/faces/{chips['people'][0]['sample']}/thumb").status_code == 200


def test_filter_by_one_person(named):
    ann = named.ids["Ann"]
    data = photos(named, person=ann)
    assert data["total"] > 0 and all(any(p["id"] == ann for p in photo["people"]) for photo in data["photos"])


def test_several_people_all_vs_any(named):
    ann, bob = named.ids["Ann"], named.ids["Bob"]
    both = photos(named, person=f"{ann},{bob}", match="all")["total"]
    either = photos(named, person=f"{ann},{bob}", match="any")["total"]
    assert both == 0 and either == photos(named, person=ann)["total"] + photos(named, person=bob)["total"]


def test_two_people_in_one_photo_are_found_by_all(named):
    ann, bob = named.ids["Ann"], named.ids["Bob"]
    photo = named.index.add_photo(scanned=2)
    a = named.index.add_face(0, photo_id=photo); b = named.index.add_face(1, photo_id=photo); named.index.commit()
    named.ok(named.post("/api/faces/assign", face_ids=[a], person_id=ann))
    named.ok(named.post("/api/faces/assign", face_ids=[b], person_id=bob))
    data = photos(named, person=f"{ann},{bob}", match="all")
    assert data["total"] == 1 and {p["id"] for p in data["photos"][0]["people"]} == {ann, bob}


def test_uncertain_matches_are_only_included_when_asked(named):
    ann = named.ids["Ann"]
    sure = photos(named, person=ann, uncertain="false")["total"]
    with_unsure = photos(named, person=ann, uncertain="true")["total"]
    assert with_unsure >= sure


@pytest.mark.parametrize("name", ["unlabeled", "review", "noface", "unscanned"])
def test_quick_filter_counts_match_the_overview(named, name):
    assert photos(named, filter=name)["total"] == named.j("/api/library")["counts"][name]


def test_noface_and_unscanned_filters_return_the_right_photos(named):
    assert {p["id"] for p in photos(named, filter="noface")["photos"]} == {named.noface}
    assert {p["id"] for p in photos(named, filter="unscanned")["photos"]} == {named.unscanned}


def test_unlabeled_excludes_photos_whose_faces_are_all_named(named):
    labeled = {p["id"] for p in photos(named, person=named.ids["Ann"])["photos"]}
    unlabeled = {p["id"] for p in photos(named, filter="unlabeled")["photos"]}
    assert not (labeled & unlabeled) - {p["id"] for p in photos(named, person=named.ids["Ann"])["photos"] if p["unlabeled"]}


def test_year_month_and_search_filters(named):
    lib = named.j("/api/library")
    year = lib["years"][0]
    assert photos(named, year=year["year"])["total"] == year["count"]
    month = year["months"][0]
    assert photos(named, year=year["year"], month=month["month"])["total"] == month["count"]
    assert photos(named, q="img0001")["total"] == 1
    assert photos(named, q="zzzzzz")["total"] == 0
    assert photos(named, q="%")["total"] == 0                        # a literal %, not a wildcard


def test_filters_combine(named):
    ann = named.ids["Ann"]
    year = photos(named, person=ann)["photos"][0]["taken"][:4]
    combined = photos(named, person=ann, year=year)
    assert 0 < combined["total"] <= photos(named, person=ann)["total"]


@pytest.mark.parametrize("params", ["filter=bogus", "person=abc", "match=weird", "order=sideways"])
def test_bad_parameters_do_not_crash(named, params):
    assert named.get(f"/api/photos?{params}").status_code in (200, 400, 422)


def test_photo_detail_lists_faces_boxes_and_names(named):
    ann = named.ids["Ann"]
    photo = photos(named, person=ann)["photos"][0]
    detail = named.j(f"/api/photos/{photo['id']}")
    assert detail["name"].endswith(".jpg") and detail["taken_at"] and detail["scanned"] is True
    face = detail["faces"][0]
    assert len(face["box"]) == 4 and all(0 <= v <= 1 for v in face["box"]) and face["name"] == "Ann" and face["person_id"] == ann
    assert named.j(f"/api/photos/{named.noface}")["faces"] == []
    assert named.j(f"/api/photos/{named.unscanned}")["scanned"] is False
    assert named.get("/api/photos/9999999").status_code == 404


# --------------------------------------------------------------------------- thumbnails and viewer images
def test_thumbnails_are_made_once_and_cached(named, env):
    photo = photos(named)["photos"][0]["id"]
    r = named.get(f"/api/photos/{photo}/thumb")
    assert r.status_code == 200 and r.content[:2] == b"\xff\xd8" and "max-age" in r.headers["cache-control"]
    cached = list((env.db.parent / "thumbs").rglob("*_360.jpg"))
    assert len(cached) == 1
    os.utime(cached[0], (1, 1))
    before = cached[0].read_bytes()
    assert named.get(f"/api/photos/{photo}/thumb").content == before


def test_thumbnail_survives_a_file_rename(named, env):
    photo = photos(named)["photos"][0]
    named.get(f"/api/photos/{photo['id']}/thumb")
    named.ok(named.post(f"/api/photos/{photo['id']}/rename", name="newname"))
    assert named.get(f"/api/photos/{photo['id']}/thumb").status_code == 200
    assert len(list((env.db.parent / "thumbs").rglob("*.jpg"))) == 1


def test_viewer_image_respects_the_size_request(named):
    photo = photos(named)["photos"][0]["id"]
    big = Image.open(io.BytesIO(named.get(f"/api/photos/{photo}/image?side=3000").content))
    small = Image.open(io.BytesIO(named.get(f"/api/photos/{photo}/image?side=600").content))
    assert max(small.size) <= 600 and max(big.size) <= 3000
    assert named.get(f"/api/photos/{photo}/image?side=50").status_code == 200        # clamped up, not an error


def test_pictures_for_missing_files_are_helpful_404s(named):
    photo = photos(named)["photos"][0]
    os.remove(named.j(f"/api/photos/{photo['id']}")["path"])
    for kind in ("thumb", "image"):
        r = named.get(f"/api/photos/{photo['id']}/{kind}")
        assert r.status_code == 404 and "refresh" in r.text
    assert named.get("/api/photos/9999999/thumb").status_code == 404 and named.get("/api/photos/9999999/image").status_code == 404


def test_a_corrupt_file_is_a_500_with_a_reason_not_a_crash(named):
    photo = photos(named)["photos"][0]
    Path(named.j(f"/api/photos/{photo['id']}")["path"]).write_bytes(b"not an image")
    r = named.get(f"/api/photos/{photo['id']}/image")
    assert r.status_code == 500 and "Could not open" in r.text


# --------------------------------------------------------------------------- rename
def _path(ui, photo_id):
    return Path(ui.j(f"/api/photos/{photo_id}")["path"])


def test_rename_keeps_the_extension_and_updates_disk_and_index(named):
    photo = photos(named)["photos"][0]["id"]
    old = _path(named, photo)
    r = named.ok(named.post(f"/api/photos/{photo}/rename", name="Grandma 80th"))
    assert r["name"] == "Grandma 80th.jpg" and r["changed"] is True
    assert (old.parent / "Grandma 80th.jpg").exists() and not old.exists()
    assert named.j(f"/api/photos/{photo}")["name"] == "Grandma 80th.jpg"


@pytest.mark.parametrize("typed, expected", [("lake.jpg", "lake.jpg"), ("LAKE.JPG", "LAKE.jpg"), ("  spaced  ", "spaced.jpg"), ("dots.in.name", "dots.in.name.jpg")])
def test_rename_typed_extension_is_not_doubled(named, typed, expected):
    photo = photos(named)["photos"][0]["id"]
    assert named.ok(named.post(f"/api/photos/{photo}/rename", name=typed))["name"] == expected


def test_rename_to_the_same_name_changes_nothing(named):
    photo = photos(named)["photos"][0]["id"]
    current = named.j(f"/api/photos/{photo}")["name"]
    assert named.ok(named.post(f"/api/photos/{photo}/rename", name=current))["changed"] is False


def test_rename_only_changing_the_case_is_allowed(named):
    photo = photos(named)["photos"][0]["id"]
    named.ok(named.post(f"/api/photos/{photo}/rename", name="abc"))
    assert named.ok(named.post(f"/api/photos/{photo}/rename", name="ABC"))["changed"] is True
    assert named.j(f"/api/photos/{photo}")["name"] == "ABC.jpg"


@pytest.mark.parametrize("bad", ["", "   ", "a/b", "a\\b", "what?", "star*", 'quo"te', "a<b", "a>b", "a|b", "a:b", "..", ".", "CON", "nul", "com1", "lpt9.txt", "ends.", "x" * 200, "tab\there"])
def test_rename_refuses_names_windows_would_not_allow(named, bad):
    photo = photos(named)["photos"][0]["id"]
    before = _path(named, photo)
    r = named.post(f"/api/photos/{photo}/rename", name=bad)
    assert r.status_code == 400, bad
    assert before.exists() and _path(named, photo) == before


def test_rename_refuses_to_overwrite_another_file(named):
    a, b = [p["id"] for p in photos(named)["photos"][:2]]
    other = _path(named, b)
    existing = named.j(f"/api/photos/{b}")["name"]
    folder_peer = [p for p in photos(named)["photos"] if _path(named, p["id"]).parent == _path(named, a).parent and p["id"] != a]
    peer = folder_peer[0]["id"] if folder_peer else None
    if peer is None:
        make_photo(_path(named, a).parent / "peer.jpg", 4242)
        target = "peer"
    else:
        target = named.j(f"/api/photos/{peer}")["name"]
    r = named.post(f"/api/photos/{a}/rename", name=target)
    assert r.status_code == 409 and "already" in r.text


def test_rename_of_a_missing_or_unknown_photo(named):
    photo = photos(named)["photos"][0]["id"]
    os.remove(_path(named, photo))
    assert named.post(f"/api/photos/{photo}/rename", name="x").status_code == 404
    assert named.post("/api/photos/9999999/rename", name="x").status_code == 404


def test_rename_needs_the_header(named):
    photo = photos(named)["photos"][0]["id"]
    assert named.client.post(f"/api/photos/{photo}/rename", json={"name": "x"}).status_code == 403


def test_clean_file_name_unit():
    from photo_browse import clean_file_name
    from fastapi import HTTPException
    assert clean_file_name("a", ".JPG") == "a.JPG" and clean_file_name("a.jpg", ".JPG") == "a.JPG"
    with pytest.raises(HTTPException):
        clean_file_name("bad/name", ".jpg")


# --------------------------------------------------------------------------- delete (to the trash folder)
def test_delete_moves_the_file_to_the_trash_and_forgets_it(named, env):
    photo = photos(named, person=named.ids["Ann"])["photos"][0]
    path = _path(named, photo["id"])
    faces_before = named.summary()["faces"]
    r = named.ok(named.post(f"/api/photos/{photo['id']}/delete"))
    trash = env.dest / sp.TRASH_DIR
    assert not path.exists() and (trash / path.name).exists() and r["was_missing"] is False
    assert path.name in (trash / "deleted.log").read_text(encoding="utf-8") and str(path) in (trash / "deleted.log").read_text(encoding="utf-8")
    assert named.get(f"/api/photos/{photo['id']}").status_code == 404
    assert named.summary()["faces"] == faces_before - photo["faces"] and named.summary()["photos"] == 77


def test_delete_numbers_the_file_when_the_trash_already_has_that_name(named, env):
    one, two = [p["id"] for p in photos(named)["photos"][:2]]
    first = _path(named, one)
    twin = first.parent / "twin.jpg"
    make_photo(twin, 31337)
    conn = __import__("sqlite3").connect(env.db)
    conn.execute("INSERT INTO photos (sha256, original_path, sorted_path, taken_at, date_source, size_bytes, sorted_at, mtime_ns) VALUES ('tw','x',?,?,?,?,?,?)",
                 (str(twin), "2020-01-01T00:00:00", "exif", 1, "now", 1))
    twin_id = conn.execute("SELECT id FROM photos WHERE sha256='tw'").fetchone()[0]
    conn.commit(); conn.close()
    named.ok(named.post(f"/api/photos/{one}/delete"))
    shutil_copy = first.parent / first.name
    make_photo(shutil_copy, 777)                                       # a new file with the same name as the deleted one
    conn = __import__("sqlite3").connect(env.db)
    conn.execute("UPDATE photos SET sorted_path=? WHERE id=?", (str(shutil_copy), twin_id)); conn.commit(); conn.close()
    named.ok(named.post(f"/api/photos/{twin_id}/delete"))
    names = sorted(p.name for p in (env.dest / sp.TRASH_DIR).glob("*.jpg"))
    assert len(names) == 2 and any("(2)" in n for n in names)


def test_delete_of_an_already_missing_file_just_cleans_the_index(named):
    photo = photos(named)["photos"][0]["id"]
    os.remove(_path(named, photo))
    assert named.ok(named.post(f"/api/photos/{photo}/delete"))["was_missing"] is True
    assert named.get(f"/api/photos/{photo}").status_code == 404


def test_delete_removes_unnamed_people_left_without_faces(named):
    small = named.index.add_photo(scanned=1)
    face = named.index.add_face(None, photo_id=small); named.index.commit()
    pid = named.ok(named.post("/api/faces/assign", face_ids=[face], new_person=True, name=""))["person_id"]
    named.index.conn.execute("UPDATE persons SET locked = 0 WHERE id = ?", (pid,)); named.index.commit()   # as a cluster-made group
    named.ok(named.post(f"/api/photos/{small}/delete"))
    assert named.get(f"/api/people/{pid}").status_code == 404


def test_delete_keeps_named_people(named):
    ann = named.ids["Ann"]
    for photo in photos(named, person=ann)["photos"]:
        named.ok(named.post(f"/api/photos/{photo['id']}/delete"))
    assert named.get(f"/api/people/{ann}").status_code == 200


def test_delete_unknown_photo_is_404_and_needs_the_header(named):
    assert named.post("/api/photos/9999999/delete").status_code == 404
    assert named.client.post("/api/photos/1/delete").status_code == 403


def test_delete_failure_leaves_everything_as_it_was(named, monkeypatch):
    photo = photos(named)["photos"][0]["id"]
    path = _path(named, photo)
    import photo_browse
    real = os.rename
    monkeypatch.setattr(photo_browse.os, "rename", lambda *a, **k: (_ for _ in ()).throw(PermissionError("locked by another program")))
    r = named.post(f"/api/photos/{photo}/delete")
    monkeypatch.setattr(photo_browse.os, "rename", real)
    assert r.status_code == 500 and "locked" in r.text and path.exists()
    assert named.get(f"/api/photos/{photo}").status_code == 200


def test_trash_folder_is_next_to_the_year_folders(named, env):
    photo = photos(named)["photos"][0]["id"]
    named.ok(named.post(f"/api/photos/{photo}/delete"))
    assert (env.dest / sp.TRASH_DIR).is_dir() and not any(sp.TRASH_DIR in p.parts for p in (env.dest / "2018").rglob("*"))


def test_refresh_ignores_what_was_deleted_in_the_ui(named, env, cli):
    photo = photos(named)["photos"][0]["id"]
    named.ok(named.post(f"/api/photos/{photo}/delete"))
    named.index.close()
    r = cli("refresh", env.dest)
    assert "Photos scanned:" in r.out
    assert scalar(env.db, "SELECT COUNT(*) FROM photos WHERE sorted_path LIKE ?", f"%{sp.TRASH_DIR}%") == 0


# --------------------------------------------------------------------------- deleting several at once
def test_bulk_delete(named, env):
    ids = [p["id"] for p in photos(named)["photos"][:5]]
    paths = [_path(named, i) for i in ids]
    r = named.ok(named.post("/api/photos/delete", photo_ids=ids))
    assert sorted(r["deleted"]) == sorted(ids) and r["failed"] == []
    assert not any(p.exists() for p in paths) and len(list((env.dest / sp.TRASH_DIR).glob("*.jpg"))) == 5
    assert named.summary()["photos"] == 73


def test_bulk_delete_handles_duplicates_unknown_ids_and_partial_failure(named, monkeypatch):
    ids = [p["id"] for p in photos(named)["photos"][:3]]
    import photo_browse
    real = os.rename
    victim = _path(named, ids[1]).name

    def flaky(src, dst):
        if Path(src).name == victim:
            raise PermissionError("in use")
        return real(src, dst)

    monkeypatch.setattr(photo_browse.os, "rename", flaky)
    r = named.ok(named.post("/api/photos/delete", photo_ids=ids + [ids[0], 9999999]))
    assert sorted(r["deleted"]) == sorted([ids[0], ids[2], 9999999]) and [f["id"] for f in r["failed"]] == [ids[1]]
    assert "in use" in r["failed"][0]["error"] and named.get(f"/api/photos/{ids[1]}").status_code == 200


def test_bulk_delete_validation(named):
    assert named.post("/api/photos/delete", photo_ids=[]).status_code == 400
    assert named.post("/api/photos/delete", photo_ids=list(range(1, 502))).status_code == 400
    assert named.post("/api/photos/delete").status_code == 422
    assert named.client.post("/api/photos/delete", json={"photo_ids": [1]}).status_code == 403
    assert named.summary()["photos"] == 78
