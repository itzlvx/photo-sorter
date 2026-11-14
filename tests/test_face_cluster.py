"""Phase 3: grouping faces into people. Faces have known identities, so we can check the grouping is right."""
import sqlite3
from collections import Counter

import numpy as np
import pytest

import sort_photos as sp
from conftest import cluster_args, connect, scalar, unit
import face_cluster as fc


def assignments(db):
    conn = connect(db)
    try:
        return {r["face_id"]: (r["person_id"], r["status"]) for r in conn.execute("SELECT * FROM face_person")}
    finally:
        conn.close()


def person_of_identity(index, identity, db=None):
    """The person ids that this identity's faces ended up in (ideally exactly one)."""
    rows = assignments(index.db)
    return Counter(rows[f][0] for f in index.faces[identity] if f in rows and rows[f][1] in ("auto", "confirmed"))


def run(index, *argv):
    return fc.cluster_faces(cluster_args(db=index.db, apply=True, **dict(zip(argv[::2], argv[1::2]))))


# --------------------------------------------------------------------------- refusals
def test_no_index(env, cli):
    r = cli("cluster", "--apply")
    assert r.code == 2 and "no index yet" in r.err.lower()


def test_index_without_faces(env, cli):
    env.db.parent.mkdir(parents=True)
    sp.open_index(env.db).close()
    r = cli("cluster", "--apply")
    assert r.code == 2 and "No faces are stored yet" in r.err


@pytest.mark.parametrize("flags, message", [
    (["--threshold", "0.3", "--review-threshold", "0.5"], "Thresholds must satisfy"),
    (["--threshold", "1.0"], "Thresholds must satisfy"),
    (["--review-threshold", "0"], "Thresholds must satisfy"),
    (["--min-group-size", "0"], "at least 1"),
])
def test_invalid_settings_are_refused(env, cli, face_index, flags, message):
    face_index(5)
    r = cli("cluster", "--apply", *flags)
    assert r.code == 2 and message in r.err


# --------------------------------------------------------------------------- the grouping itself
def test_dry_run_saves_nothing(env, cli, face_index):
    face_index(8, 6)
    r = cli("cluster")
    assert r.code == 0 and "Dry run" in r.out
    assert scalar(env.db, "SELECT COUNT(*) FROM persons") == 0 and scalar(env.db, "SELECT COUNT(*) FROM face_person") == 0


def test_each_identity_becomes_one_person(env, cli, face_index):
    index = face_index(30, 20, 12)
    assert cli("cluster", "--apply").code == 0
    for identity in range(3):
        groups = person_of_identity(index, identity)
        assert len(groups) == 1 and sum(groups.values()) >= len(index.faces[identity]) * 0.9, (identity, groups)
    assert len({next(iter(person_of_identity(index, i))) for i in range(3)}) == 3     # three different people
    assert scalar(env.db, "SELECT COUNT(*) FROM persons") == 3


def test_strangers_are_not_turned_into_people(env, cli, face_index):
    index = face_index(15, strangers=12)
    cli("cluster", "--apply")
    states = Counter(s for (_, s) in assignments(env.db).values())
    assert states["unassigned"] >= 10 and scalar(env.db, "SELECT COUNT(*) FROM persons") == 1


def test_groups_smaller_than_the_minimum_are_dropped(env, cli, face_index):
    face_index(12, 2)
    cli("cluster", "--apply")
    assert scalar(env.db, "SELECT COUNT(*) FROM persons") == 1
    r = cli("cluster", "--apply", "--min-group-size", "2")
    assert scalar(env.db, "SELECT COUNT(*) FROM persons") == 2


def test_every_face_gets_a_status_and_unplaced_ones_a_reason(env, cli, face_index):
    index = face_index(10, strangers=5)
    cli("cluster", "--apply")
    rows = assignments(env.db)
    assert len(rows) == scalar(env.db, "SELECT COUNT(*) FROM faces")
    reasons = [r[0] for r in connect(env.db).execute("SELECT reason FROM face_person WHERE status != 'auto'")]
    assert reasons and all(reasons)


def test_low_quality_faces_are_not_placed_with_confidence(env, cli, face_index):
    index = face_index(15)
    for _ in range(6):
        index.add_face(0, px=30, sigma=1.5)               # tiny, noisy faces of identity 0
    index.commit()
    cli("cluster", "--apply")
    tiny = index.faces[0][-6:]
    states = Counter(assignments(env.db)[f][1] for f in tiny)
    assert states["auto"] == 0 and sum(states.values()) == 6


def test_faces_below_detection_score_cannot_define_a_person(env, cli, face_index):
    face_index(10, det=0.3)
    cli("cluster", "--apply")
    assert scalar(env.db, "SELECT COUNT(*) FROM persons") == 0


def test_running_twice_changes_nothing(env, cli, face_index):
    face_index(20, 14, strangers=5)
    cli("cluster", "--apply")
    first = assignments(env.db)
    cli("cluster", "--apply")
    assert assignments(env.db) == first


def test_new_faces_join_an_existing_named_person_instead_of_creating_a_duplicate(env, cli, face_index):
    index = face_index(25, 15)
    cli("cluster", "--apply")
    ann = next(iter(person_of_identity(index, 0)))
    conn = connect(env.db); conn.execute("UPDATE persons SET name='Ann' WHERE id=?", (ann,)); conn.commit(); conn.close()
    before_people = scalar(env.db, "SELECT COUNT(*) FROM persons")
    new = [index.add_face(0) for _ in range(5)]
    index.commit()
    cli("cluster", "--apply")
    rows = assignments(env.db)
    assert all(rows[f][0] == ann for f in new) and scalar(env.db, "SELECT COUNT(*) FROM persons") == before_people


def test_confirmed_faces_never_move(env, cli, face_index):
    index = face_index(25, 15)
    cli("cluster", "--apply")
    ann = next(iter(person_of_identity(index, 0)))
    odd_face = index.faces[1][0]                         # a face of someone else that I force into Ann
    conn = connect(env.db)
    conn.execute("UPDATE face_person SET person_id=?, status='confirmed' WHERE face_id=?", (ann, odd_face))
    conn.commit(); conn.close()
    cli("cluster", "--apply", "--recluster")
    assert assignments(env.db)[odd_face] == (ann, "confirmed")


def test_recluster_keeps_named_people_and_rebuilds_unnamed_groups(env, cli, face_index):
    index = face_index(25, 18, 12)
    cli("cluster", "--apply")
    ann = next(iter(person_of_identity(index, 0)))
    conn = connect(env.db); conn.execute("UPDATE persons SET name='Ann' WHERE id=?", (ann,)); conn.commit(); conn.close()
    cli("cluster", "--apply", "--recluster")
    assert scalar(env.db, "SELECT name FROM persons WHERE id=?", ann) == "Ann"
    assert scalar(env.db, "SELECT COUNT(*) FROM persons") == 3
    assert len(person_of_identity(index, 0)) == 1 and ann in person_of_identity(index, 0)


def test_locked_people_survive_recluster_even_without_a_name(env, cli, face_index):
    index = face_index(25, 18)
    cli("cluster", "--apply")
    keep = next(iter(person_of_identity(index, 1)))
    conn = connect(env.db); conn.execute("UPDATE persons SET locked=1 WHERE id=?", (keep,)); conn.commit(); conn.close()
    cli("cluster", "--apply", "--recluster")
    assert scalar(env.db, "SELECT COUNT(*) FROM persons WHERE id=?", keep) == 1
    assert keep in person_of_identity(index, 1)


def test_ignored_faces_stay_ignored(env, cli, face_index):
    index = face_index(25, 15)
    cli("cluster", "--apply")
    victim = index.faces[0][0]
    conn = connect(env.db); conn.execute("UPDATE face_person SET status='ignored', person_id=NULL WHERE face_id=?", (victim,)); conn.commit(); conn.close()
    cli("cluster", "--apply", "--recluster")
    assert assignments(env.db)[victim] == (None, "ignored")


def test_a_rejected_match_is_never_suggested_again(env, cli, face_index):
    index = face_index(25, 15)
    cli("cluster", "--apply")
    ann = next(iter(person_of_identity(index, 0)))
    face = index.faces[0][3]
    conn = connect(env.db)
    conn.execute("INSERT INTO face_rejections (face_id, person_id) VALUES (?,?)", (face, ann))
    conn.execute("DELETE FROM face_person WHERE face_id=?", (face,))
    conn.commit(); conn.close()
    cli("cluster", "--apply")
    assert assignments(env.db).get(face, (None, None))[0] != ann


def test_similar_ai_groups_merge_but_two_named_people_never_do(env, cli, face_index):
    index = face_index(20)
    a = index.centre(0)
    index.centres.append(unit(0.55 * a + np.sqrt(1 - 0.55 ** 2) * unit(np.random.default_rng(3).standard_normal(512))))
    for _ in range(14):
        index.add_face(1)
    index.commit()
    cli("cluster", "--apply")
    assert scalar(env.db, "SELECT COUNT(*) FROM persons") == 2                    # alike, but kept apart by default
    conn = connect(env.db); conn.execute("DELETE FROM face_person"); conn.execute("DELETE FROM persons"); conn.commit(); conn.close()
    cli("cluster", "--apply", "--merge-threshold", "0.40", "--threshold", "0.40", "--review-threshold", "0.30")
    assert scalar(env.db, "SELECT COUNT(*) FROM persons") == 1                    # merged when the bar is lowered
    # named people are never merged, whatever the bar
    conn = connect(env.db); conn.execute("DELETE FROM face_person"); conn.execute("DELETE FROM persons")
    for n, f in enumerate((index.faces[0][0], index.faces[1][0])):
        pid = conn.execute("INSERT INTO persons (name, created_at) VALUES (?, 'now')", (f"P{n}",)).lastrowid
        conn.execute("INSERT INTO face_person (face_id, person_id, similarity, status, reason, assigned_at) VALUES (?,?,?,?,?,?)", (f, pid, 1.0, "confirmed", "", "now"))
    conn.commit(); conn.close()
    cli("cluster", "--apply", "--merge-threshold", "0.40", "--threshold", "0.40", "--review-threshold", "0.30")
    assert scalar(env.db, "SELECT COUNT(*) FROM persons WHERE name IS NOT NULL") == 2


def test_unreadable_embeddings_are_skipped_with_a_warning(env, cli, face_index):
    index = face_index(12)
    photo = index.add_photo()
    index.commit()
    conn = connect(env.db)
    conn.execute("INSERT INTO faces (photo_id,x1,y1,x2,y2,det_score,face_px,blur,yaw,embedding,thumb,model) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                 (photo, .1, .1, .5, .5, .9, 100, 100., 5., b"short", None, "buffalo_l"))
    conn.commit(); conn.close()
    r = cli("cluster", "--apply")
    assert r.code == 0 and "unreadable" in r.err and scalar(env.db, "SELECT COUNT(*) FROM persons") == 1


def test_model_selection(env, cli, face_index):
    index = face_index(12)
    for _ in range(4):
        index.add_face(1, model="other_model")
    index.commit()
    r = cli("cluster", "--apply")                                  # defaults to the model with the most faces
    assert "model buffalo_l" in r.out and scalar(env.db, "SELECT COUNT(*) FROM persons") == 1
    r = cli("cluster", "--apply", "--model", "nonexistent")
    assert r.code == 2


def test_a_second_scan_batch_is_grouped_without_disturbing_the_first(env, cli, face_index):
    index = face_index(25, 15)
    cli("cluster", "--apply")
    first = assignments(env.db)
    for _ in range(10):
        index.add_face(0)
    index.commit()
    cli("cluster", "--apply")
    now = assignments(env.db)
    assert all(now[f] == first[f] for f in first)                  # earlier decisions untouched


def test_emptied_unnamed_people_are_removed(env, cli, face_index):
    index = face_index(25, 15)
    cli("cluster", "--apply")
    conn = connect(env.db)
    loser = next(iter(person_of_identity(index, 1)))
    conn.execute("DELETE FROM faces WHERE id IN (SELECT face_id FROM face_person WHERE person_id=?)", (loser,))
    conn.commit(); conn.close()
    cli("cluster", "--apply", "--recluster")
    assert scalar(env.db, "SELECT COUNT(*) FROM persons") == 1


def test_a_larger_library_is_grouped_correctly_and_quickly(env, cli, face_index):
    import time
    index = face_index(120, 90, 70, 60, 40, strangers=60, files=False)
    started = time.time()
    assert cli("cluster", "--apply").code == 0
    assert time.time() - started < 30
    for identity in range(5):
        groups = person_of_identity(index, identity)
        assert len(groups) == 1, groups
    assert scalar(env.db, "SELECT COUNT(*) FROM persons") == 5
