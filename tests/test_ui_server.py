"""The web interface API (phase 4): safety checks, reading people and faces, and every way of changing them."""
import pytest

from conftest import cluster_args, connect, scalar

pytest.importorskip("fastapi")


# --------------------------------------------------------------------------- safety
def test_other_host_names_are_refused(ui):
    from fastapi.testclient import TestClient
    assert TestClient(ui.app, base_url="http://evil.example").get("/api/summary").status_code == 403
    assert ui.get("/api/summary", headers={"Host": "127.0.0.1.evil.com"}).status_code == 403
    assert ui.get("/api/summary", headers={"Host": "evil.com:8765"}).status_code == 403


@pytest.mark.parametrize("host", ["127.0.0.1:8765", "localhost:8765", "localhost", "[::1]:8765"])
def test_loopback_names_are_accepted(ui, host):
    assert ui.get("/api/summary", headers={"Host": host}).status_code == 200


def test_changes_need_the_custom_header(ui):
    r = ui.client.post("/api/faces/ignore", json={"face_ids": [1]})
    assert r.status_code == 403 and ui.summary()["ignored"] == 0
    assert ui.client.post("/api/faces/ignore", json={"face_ids": [1]}, headers={"X-Photo-Sorter": "0"}).status_code == 403


def test_api_docs_are_not_exposed(ui):
    assert ui.get("/docs").status_code == 404 and ui.get("/openapi.json").status_code == 404 and ui.get("/redoc").status_code == 404


def test_responses_carry_safety_headers(ui):
    r = ui.get("/api/summary")
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["referrer-policy"] == "no-referrer"


def test_page_and_static_files_are_served(ui):
    page = ui.get("/")
    assert page.status_code == 200 and "text/html" in page.headers["content-type"] and "no-cache" in page.headers["cache-control"]
    for name in ("app.js", "style.css"):
        assert ui.get(f"/web/{name}").status_code == 200
    assert ui.get("/web/../sort_photos.py").status_code in (404, 400)


def test_any_host_mode_allows_other_names(env, ui):
    import ui_server
    from fastapi.testclient import TestClient
    app = ui_server.create_app(env.db, any_host=True)
    assert TestClient(app, base_url="http://nas.local").get("/api/summary").status_code == 200


# --------------------------------------------------------------------------- reading
def test_summary_counts(ui):
    s = ui.summary()
    assert s["faces"] == 76 and s["people"] == 3 and s["named"] == 0 and s["unnamed"] == 3
    assert s["unassigned"] >= 6 and s["review"] + s["unassigned"] + s["auto"] + s["confirmed"] == 76
    assert s["photos"] == 76 and s["ungrouped"] == 0 and s["db_path"].endswith("photo_index.db")


def test_people_are_listed_biggest_first_with_samples(ui):
    people = ui.people()
    sizes = [p["faces"] for p in people]
    assert sizes == sorted(sizes, reverse=True) and len(people) == 3
    assert all(1 <= len(p["samples"]) <= 4 for p in people)
    assert ui.get(f"/api/faces/{people[0]['samples'][0]}/thumb").status_code == 200


def test_people_paging_and_limits(ui):
    assert ui.j("/api/people?limit=2")["total"] == 3 and len(ui.j("/api/people?limit=2")["people"]) == 2
    assert len(ui.j("/api/people?limit=2&offset=2")["people"]) == 1
    assert len(ui.j("/api/people?limit=0")["people"]) == 1          # clamped to at least 1


def test_people_filters_and_search(ui):
    first = ui.people()[0]
    ui.ok(ui.post(f"/api/people/{first['id']}/name", name="Anna_Maria 100%"))
    assert ui.j("/api/people?named=named")["total"] == 1 and ui.j("/api/people?named=unnamed")["total"] == 2
    assert ui.j("/api/people?q=anna")["total"] == 1                  # case-insensitive
    assert ui.j("/api/people?q=%25")["total"] == 1                   # a literal % in the name, not a wildcard
    assert ui.j("/api/people?q=zzz")["total"] == 0
    assert ui.j("/api/people?q=a_a")["total"] == 0                   # underscore is literal too


@pytest.mark.parametrize("sort", ["size", "name", "review", "recent", "nonsense"])
def test_people_sort_modes_do_not_fail(ui, sort):
    assert ui.j(f"/api/people?sort={sort}")["total"] == 3


def test_unknown_person_is_404(ui):
    assert ui.get("/api/people/99999").status_code == 404
    assert ui.j("/api/people/99999/similar") == {"people": []}


def test_chips_route_is_not_swallowed_by_the_person_route(ui):
    assert "unnamed_groups" in ui.j("/api/people/chips")


def test_face_scopes(ui):
    assert len(ui.faces(scope="members")) == ui.summary()["auto"] + ui.summary()["confirmed"]
    assert len(ui.faces(scope="review")) == ui.summary()["review"] > 0
    assert len(ui.faces(scope="unassigned")) == ui.summary()["unassigned"]
    assert ui.faces(scope="ignored") == []
    assert ui.get("/api/faces?scope=bogus").status_code == 400


def test_face_filters_and_order(ui):
    person = ui.people()[0]["id"]
    mine = ui.faces(scope="members", person_id=person)
    assert mine and all(f["person_id"] == person for f in mine)
    worst, best = ui.faces(scope="members", order="worst"), ui.faces(scope="members", order="best")
    assert worst[0]["similarity"] <= worst[-1]["similarity"] and best[0]["similarity"] >= best[-1]["similarity"]
    assert len(ui.faces(scope="unassigned", person_id="none")) == ui.summary()["unassigned"]
    assert all(f["reason"] for f in ui.faces(scope="unassigned") if f["reason"] is not None)
    assert ui.faces(scope="members", order="date") and ui.faces(scope="members", order="id")


def test_face_paging_is_clamped(ui):
    assert len(ui.j("/api/faces?limit=5")["faces"]) == 5
    assert len(ui.j("/api/faces?limit=100000")["faces"]) <= 500


def test_review_buckets_group_by_suggested_person(ui):
    buckets = ui.j("/api/review/buckets")["buckets"]
    assert sum(b["count"] for b in buckets) == ui.summary()["review"] and buckets
    assert any(b["person_id"] is not None for b in buckets)


def test_face_thumbnail_info_and_photo(ui):
    face = ui.faces(scope="members")[0]["id"]
    thumb = ui.get(f"/api/faces/{face}/thumb")
    assert thumb.status_code == 200 and thumb.headers["content-type"] == "image/jpeg" and thumb.content[:2] == b"\xff\xd8"
    info = ui.j(f"/api/faces/{face}")
    assert info["path"].endswith(".jpg") and info["faces_in_photo"] == 1
    photo = ui.get(f"/api/faces/{face}/photo")
    assert photo.status_code == 200 and photo.content[:2] == b"\xff\xd8"
    assert ui.get("/api/faces/999999/thumb").status_code == 404 and ui.get("/api/faces/999999").status_code == 404


def test_face_photo_for_a_missing_file_is_a_helpful_404(ui):
    face = ui.faces(scope="members")[0]
    __import__("os").remove(ui.j(f"/api/faces/{face['id']}")["path"])
    r = ui.get(f"/api/faces/{face['id']}/photo")
    assert r.status_code == 404 and "refresh" in r.text.lower()


def test_similar_people_and_suggestions(ui):
    a, b = ui.people()[:2]
    assert isinstance(ui.j(f"/api/people/{a['id']}/similar")["people"], list)
    suggestions = ui.ok(ui.post("/api/faces/suggest", face_ids=ui.faces(scope="review")[:3] and [f["id"] for f in ui.faces(scope="review")[:3]]))
    assert "people" in suggestions
    assert ui.ok(ui.post("/api/faces/suggest", face_ids=[999999])) == {"people": []}


# --------------------------------------------------------------------------- naming and merging people
def test_naming_a_person_saves_the_name_and_protects_them(ui):
    pid = ui.people()[0]["id"]
    assert ui.ok(ui.post(f"/api/people/{pid}/name", name="  Ann   Lee  "))["name"] == "Ann Lee"
    person = ui.j(f"/api/people/{pid}")
    assert person["name"] == "Ann Lee" and person["locked"] is True


def test_clearing_a_name(ui):
    pid = ui.people()[0]["id"]
    ui.ok(ui.post(f"/api/people/{pid}/name", name="Ann"))
    assert ui.ok(ui.post(f"/api/people/{pid}/name", name="   "))["name"] == ""
    assert ui.j(f"/api/people/{pid}")["name"] == ""


def test_a_name_already_used_gives_a_409_with_the_other_person(ui):
    a, b = ui.people()[:2]
    ui.ok(ui.post(f"/api/people/{a['id']}/name", name="Ann"))
    r = ui.post(f"/api/people/{b['id']}/name", name="ann")                   # case-insensitive
    assert r.status_code == 409 and r.json()["conflict_id"] == a["id"]
    assert ui.j(f"/api/people/{b['id']}")["name"] == ""


def test_naming_with_merge_if_exists_merges_the_two(ui):
    a, b = ui.people()[:2]
    ui.ok(ui.post(f"/api/people/{a['id']}/name", name="Ann"))
    result = ui.ok(ui.post(f"/api/people/{b['id']}/name", name="Ann", merge_if_exists=True))
    assert result["merged_into"] == a["id"]
    assert ui.get(f"/api/people/{b['id']}").status_code == 404
    assert ui.j(f"/api/people/{a['id']}")["faces"] == a["faces"] + b["faces"]


def test_naming_an_unknown_person_is_404(ui):
    assert ui.post("/api/people/99999/name", name="Nobody").status_code == 404


def test_merge_moves_faces_and_adopts_the_name(ui):
    a, b = ui.people()[:2]
    ui.ok(ui.post(f"/api/people/{b['id']}/name", name="Bea"))
    ui.ok(ui.post(f"/api/people/{a['id']}/merge", into=b["id"]))
    target = ui.j(f"/api/people/{b['id']}")
    assert target["faces"] == a["faces"] + b["faces"] and target["name"] == "Bea" and target["locked"] is True
    assert ui.get(f"/api/people/{a['id']}").status_code == 404


def test_merge_keeps_the_target_name_and_adopts_the_source_name_if_target_has_none(ui):
    a, b = ui.people()[:2]
    ui.ok(ui.post(f"/api/people/{a['id']}/name", name="Source"))
    ui.ok(ui.post(f"/api/people/{a['id']}/merge", into=b["id"]))
    assert ui.j(f"/api/people/{b['id']}")["name"] == "Source"


def test_merge_into_self_or_unknown_is_refused(ui):
    a = ui.people()[0]["id"]
    assert ui.post(f"/api/people/{a}/merge", into=a).status_code == 400
    assert ui.post(f"/api/people/{a}/merge", into=99999).status_code == 404
    assert ui.post("/api/people/99999/merge", into=a).status_code == 404


# --------------------------------------------------------------------------- changing faces
def test_confirm_marks_faces_as_confirmed_and_protects_the_person(ui):
    review = [f for f in ui.faces(scope="review") if f["person_id"] is not None][:3]
    result = ui.ok(ui.post("/api/faces/confirm", face_ids=[f["id"] for f in review]))
    assert result["changed"] == 3 and result["skipped"] == 0
    assert scalar(ui.db, "SELECT COUNT(*) FROM face_person WHERE status='confirmed'") == 3
    assert ui.j(f"/api/people/{review[0]['person_id']}")["locked"] is True


def test_confirm_skips_unassigned_and_ignored_faces(ui):
    stranger = ui.faces(scope="unassigned")[0]["id"]
    assert ui.ok(ui.post("/api/faces/confirm", face_ids=[stranger, 999999]))["changed"] == 0


def test_assign_to_an_existing_person(ui):
    target = ui.people()[0]["id"]
    strangers = [f["id"] for f in ui.faces(scope="unassigned")[:2]]
    result = ui.ok(ui.post("/api/faces/assign", face_ids=strangers, person_id=target))
    assert result["changed"] == 2 and result["person_id"] == target
    assert all(f["status"] == "confirmed" for f in ui.faces(scope="members", person_id=target) if f["id"] in strangers)


def test_assign_to_a_new_named_person(ui):
    strangers = [f["id"] for f in ui.faces(scope="unassigned")[:2]]
    new_id = ui.ok(ui.post("/api/faces/assign", face_ids=strangers, new_person=True, name="Cousin Dan"))["person_id"]
    assert ui.j(f"/api/people/{new_id}")["name"] == "Cousin Dan" and ui.summary()["people"] == 4


def test_assign_needs_a_person(ui):
    assert ui.post("/api/faces/assign", face_ids=[1]).status_code == 400
    assert ui.post("/api/faces/assign", face_ids=[1], person_id=99999).status_code == 404


def test_moving_all_faces_away_drops_an_emptied_unnamed_person(ui):
    small = ui.people()[-1]
    other = ui.people()[0]["id"]
    ids = [f["id"] for f in ui.faces(scope="members", person_id=small["id"])]
    ui.ok(ui.post("/api/faces/assign", face_ids=ids, person_id=other))
    assert ui.get(f"/api/people/{small['id']}").status_code == 404                  # emptied and unnamed: gone


def test_a_named_person_is_kept_even_when_emptied(ui):
    small = ui.people()[-1]
    ui.ok(ui.post(f"/api/people/{small['id']}/name", name="Keep Me"))
    ids = [f["id"] for f in ui.faces(scope="members", person_id=small["id"])]
    ui.ok(ui.post("/api/faces/assign", face_ids=ids, person_id=ui.people()[0]["id"]))
    assert ui.get(f"/api/people/{small['id']}").status_code == 200
    # ...and the faces remember they are not hers, so the AI does not suggest her again
    assert scalar(ui.db, "SELECT COUNT(*) FROM face_rejections WHERE person_id=?", small["id"]) == len(ids)


def test_reject_sends_a_face_back_to_review_and_is_remembered(ui):
    face = ui.faces(scope="members")[0]
    assert ui.ok(ui.post("/api/faces/reject", face_ids=[face["id"]]))["changed"] == 1
    row = connect(ui.db).execute("SELECT * FROM face_person WHERE face_id=?", (face["id"],)).fetchone()
    assert row["status"] == "review" and row["reason"] == "rejected" and row["person_id"] is None
    assert scalar(ui.db, "SELECT COUNT(*) FROM face_rejections WHERE face_id=? AND person_id=?", face["id"], face["person_id"]) == 1


def test_ignore_and_restore(ui):
    ids = [f["id"] for f in ui.faces(scope="unassigned")[:3]]
    assert ui.ok(ui.post("/api/faces/ignore", face_ids=ids))["changed"] == 3
    assert ui.summary()["ignored"] == 3 and len(ui.faces(scope="ignored")) == 3
    assert ui.ok(ui.post("/api/faces/restore", face_ids=ids))["changed"] == 3
    assert ui.summary()["ignored"] == 0 and {f["id"] for f in ui.faces(scope="review")} >= set(ids)


def test_ignoring_a_persons_last_faces_removes_the_unnamed_person(ui):
    small = ui.people()[-1]
    ids = [f["id"] for f in ui.faces(scope="members", person_id=small["id"])]
    ui.ok(ui.post("/api/faces/ignore", face_ids=ids))
    assert ui.get(f"/api/people/{small['id']}").status_code == 404


@pytest.mark.parametrize("path", ["confirm", "assign", "reject", "ignore", "restore", "suggest"])
def test_face_endpoints_validate_the_id_list(ui, path):
    assert ui.post(f"/api/faces/{path}", face_ids=[]).status_code == 422
    assert ui.post(f"/api/faces/{path}", face_ids=list(range(1, 5002))).status_code == 422
    assert ui.post(f"/api/faces/{path}").status_code == 422


# --------------------------------------------------------------------------- regrouping from the UI
def test_regroup_places_new_faces_and_reports_progress(ui):
    pid = ui.people()[0]["id"]
    ui.ok(ui.post(f"/api/people/{pid}/name", name="Ann"))
    new = [ui.index.add_face(0) for _ in range(4)]
    ui.index.commit()
    assert ui.summary()["ungrouped"] == 4
    result = ui.ok(ui.post("/api/regroup"))
    assert result["ok"] is True and any("Saved" in line for line in result["lines"])
    assert ui.summary()["ungrouped"] == 0
    assert {f["id"] for f in ui.faces(scope="members", person_id=pid)} >= set(new)


def test_regroup_never_undoes_my_decisions(ui):
    face = ui.faces(scope="members")[0]["id"]
    ui.ok(ui.post("/api/faces/ignore", face_ids=[face]))
    ui.ok(ui.post("/api/regroup"))
    assert face in {f["id"] for f in ui.faces(scope="ignored")}


def test_regroup_refuses_to_run_twice_at_once(ui):
    assert ui.app.state.regroup_lock.acquire(blocking=False)
    try:
        assert ui.post("/api/regroup").status_code == 409
    finally:
        ui.app.state.regroup_lock.release()


def test_similarity_hints_follow_merges(ui):
    a, b, c = ui.people()[:3]
    before = ui.j(f"/api/people/{a['id']}/similar")
    ui.ok(ui.post(f"/api/people/{b['id']}/merge", into=c["id"]))
    after = ui.j(f"/api/people/{a['id']}/similar")["people"]
    assert all(p["id"] != b["id"] for p in after)
