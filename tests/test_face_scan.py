"""Phase 2 ('faces' command), tested with a stand-in for the InsightFace model so no model download or GPU is needed."""
import sqlite3
import sys
import types
from datetime import datetime

import numpy as np
import pytest
from PIL import Image

import sort_photos as sp
from conftest import FaceIndex, make_photo, scalar, stat, unit

pytest.importorskip("cv2", reason="opencv-python-headless is needed for face_scan (pip install -r requirements-dev.txt)")
import face_scan  # noqa: E402


class FakeEngine:
    """Returns scripted detections, one entry per photo scanned (a list of faces, or an exception to raise)."""
    script: list = []
    calls = 0
    fail_on_load: Exception | None = None

    def __init__(self, model, model_root, det_size, min_score, mode="auto"):
        if FakeEngine.fail_on_load:
            raise FakeEngine.fail_on_load
        self.model, self.provider = model, "CPUExecutionProvider"

    def detect(self, bgr):
        index = FakeEngine.calls
        FakeEngine.calls += 1
        step = FakeEngine.script[index] if index < len(FakeEngine.script) else []
        if isinstance(step, BaseException):
            raise step
        return step


def face(x1=10, y1=8, x2=40, y2=40, score=0.9, seed=0, yaw=5.0):
    return {"bbox": (x1, y1, x2, y2), "score": score, "embedding": unit(np.random.default_rng(seed).standard_normal(512)), "yaw": yaw}


@pytest.fixture
def engine(monkeypatch):
    FakeEngine.script, FakeEngine.calls, FakeEngine.fail_on_load = [], 0, None
    monkeypatch.setattr(face_scan, "runtime_problem", lambda: None)
    monkeypatch.setattr(face_scan, "pick_providers", lambda mode="auto": (["CPUExecutionProvider"], False))
    monkeypatch.setattr(face_scan, "FaceEngine", FakeEngine)
    return FakeEngine


@pytest.fixture
def indexed(env, cli):
    """Five sorted photos, indexed (via refresh) but not yet scanned."""
    for n in range(5):
        make_photo(env.dest / "2019" / "03-mar" / f"p{n}.jpg", n, size=(64, 48), taken=datetime(2019, 3, 1 + n))
    assert cli("refresh", env.dest, "--apply").code == 0
    return env


def count(db, table="faces"):
    return scalar(db, f"SELECT COUNT(*) FROM {table}")


def test_no_index_yet(env, cli, engine):
    r = cli("faces", "--apply")
    assert r.code == 2 and "no index yet" in r.err.lower()


def test_dry_run_counts_work_and_writes_nothing(indexed, cli, engine):
    r = cli("faces")
    assert r.code == 0 and "To scan now: 5" in r.out and "Dry run only" in r.out
    assert count(indexed.db) == 0 and engine.calls == 0


def test_missing_packages_stop_an_apply_with_install_help(indexed, cli, monkeypatch):
    monkeypatch.setattr(face_scan, "runtime_problem", lambda: "ImportError: No module named insightface")
    r = cli("faces", "--apply")
    assert r.code == 2 and "requirements-faces.txt" in r.err
    assert cli("faces").code == 0                       # a dry run only warns


def test_scan_stores_faces_with_normalised_boxes_thumbs_and_embeddings(indexed, cli, engine):
    engine.script = [[face(seed=1), face(x1=40, x2=60, seed=2)], [face(seed=3)], [], [], []]
    r = cli("faces", "--apply")
    assert r.code == 0, r.text
    assert stat(r.out, "with faces:") == 2 and stat(r.out, "without faces:") == 3 and stat(r.out, "Faces found:") == 3
    assert count(indexed.db) == 3 and count(indexed.db, "face_scans") == 5
    conn = sqlite3.connect(indexed.db)
    x1, y1, x2, y2, px, emb, thumb, model = conn.execute("SELECT x1,y1,x2,y2,face_px,embedding,thumb,model FROM faces ORDER BY id").fetchone()
    assert (round(x1, 3), round(y1, 3), round(x2, 3), round(y2, 3)) == (round(10 / 64, 3), round(8 / 48, 3), round(40 / 64, 3), round(40 / 48, 3))
    assert px == 30 and len(emb) == 2048 and thumb[:2] == b"\xff\xd8" and model == "buffalo_l"
    conn.close()


def test_second_run_only_scans_new_photos(indexed, cli, engine, env):
    engine.script = [[face()]] * 5
    cli("faces", "--apply")
    r = cli("faces", "--apply")
    assert "Nothing to scan" in r.out
    make_photo(env.dest / "2019" / "04-apr" / "new.jpg", 99, taken=datetime(2019, 4, 1))
    cli("refresh", env.dest, "--apply")
    engine.script, engine.calls = [[face(seed=9)]], 0
    r = cli("faces", "--apply")
    assert stat(r.out, "Photos processed:") == 1 and count(indexed.db) == 6


def test_limit_scans_only_that_many_and_says_more_remain(indexed, cli, engine):
    engine.script = [[face()]] * 5
    r = cli("faces", "--apply", "--limit", "2")
    assert count(indexed.db, "face_scans") == 2 and "3 more photos still to scan" in r.out
    cli("faces", "--apply")
    assert count(indexed.db, "face_scans") == 5


def test_unreadable_photo_is_recorded_as_an_error_and_others_continue(indexed, cli, engine):
    (indexed.dest / "2019" / "03-mar" / "p1.jpg").write_bytes(b"garbage")
    engine.script = [[face()]] * 5
    r = cli("faces", "--apply")
    assert r.code == 1 and stat(r.out, "  errors:") == 1 and stat(r.out, "with faces:") == 4
    assert scalar(indexed.db, "SELECT COUNT(*) FROM face_scans WHERE error IS NOT NULL") == 1
    again = cli("faces", "--apply")
    assert "Nothing to scan" in again.out                        # failed photos are skipped...
    (indexed.dest / "2019" / "03-mar" / "p1.jpg").write_bytes((indexed.dest / "2019" / "03-mar" / "p0.jpg").read_bytes())
    engine.script, engine.calls = [[face(seed=4)]], 0
    retried = cli("faces", "--apply", "--retry-errors")          # ...unless asked to retry
    assert retried.code == 0 and scalar(indexed.db, "SELECT COUNT(*) FROM face_scans WHERE error IS NOT NULL") == 0


def test_a_photo_deleted_from_disk_is_skipped_without_an_error_row(indexed, cli, engine):
    (indexed.dest / "2019" / "03-mar" / "p2.jpg").unlink()
    engine.script = [[face()]] * 5
    r = cli("faces", "--apply")
    assert r.code == 0 and "no longer exist" in r.err and count(indexed.db, "face_scans") == 4


def test_a_detector_crash_on_one_photo_is_recorded_and_scanning_goes_on(indexed, cli, engine):
    engine.script = [[face()], RuntimeError("boom"), [face()], [face()], [face()]]
    r = cli("faces", "--apply")
    assert r.code == 1 and "boom" in r.err and count(indexed.db, "face_scans") == 5
    assert scalar(indexed.db, "SELECT COUNT(*) FROM face_scans WHERE error LIKE '%boom%'") == 1


def test_repeated_detector_crashes_abort_with_advice(env, cli, engine):
    for n in range(face_scan.MAX_CONSECUTIVE_DETECT_FAILURES + 3):
        make_photo(env.dest / "2019" / "03-mar" / f"p{n}.jpg", n, taken=datetime(2019, 3, 1))
    cli("refresh", env.dest, "--apply")
    engine.script = [RuntimeError("gpu lost")] * 20
    r = cli("faces", "--apply")
    assert r.code == 1 and "Stopped early" in r.err and "--cpu" in r.err
    assert count(env.db, "face_scans") == face_scan.MAX_CONSECUTIVE_DETECT_FAILURES


def test_ctrl_c_saves_progress_and_returns_130(indexed, cli, engine):
    engine.script = [[face()], [face()], KeyboardInterrupt(), [face()], [face()]]
    r = cli("faces", "--apply")
    assert r.code == 130 and "Stopped by you" in r.err and count(indexed.db, "face_scans") == 2


def test_tiny_and_out_of_frame_boxes_are_dropped_and_big_ones_clipped(indexed, cli, engine):
    engine.script = [[face(10, 10, 11, 11), face(-20, -20, 500, 500), face(100, 100, 200, 200)]] + [[]] * 4
    cli("faces", "--apply")
    conn = sqlite3.connect(indexed.db)
    rows = conn.execute("SELECT x1,y1,x2,y2 FROM faces").fetchall()
    conn.close()
    assert rows == [(0.0, 0.0, 1.0, 1.0)]


def test_model_load_failure_is_reported(indexed, cli, engine):
    engine.fail_on_load = RuntimeError("model files are corrupt")
    r = cli("faces", "--apply")
    assert r.code == 2 and "Could not load the face model" in r.err and count(indexed.db, "face_scans") == 0


def test_gpu_required_but_unavailable_stops_with_instructions(indexed, cli, engine, monkeypatch):
    def refuse(mode="auto"):
        raise RuntimeError("--gpu was given, but the installed ONNX Runtime has no NVIDIA CUDA support")
    monkeypatch.setattr(face_scan, "pick_providers", refuse)
    r = cli("faces", "--apply", "--gpu")
    assert r.code == 2 and "--gpu was given" in r.err and count(indexed.db, "face_scans") == 0


def test_cpu_flag_selects_cpu_mode():
    import argparse
    assert face_scan.hardware_mode(argparse.Namespace(cpu=True, gpu=True)) == "cpu"
    assert face_scan.hardware_mode(argparse.Namespace(cpu=False, gpu=True)) == "gpu"
    assert face_scan.hardware_mode(argparse.Namespace(cpu=False, gpu=False)) == "auto"


# --------------------------------------------------------------------------- re-scanning and confirmed faces
def _confirm_a_face(db):
    conn = sp.open_index(db)
    face_id = conn.execute("SELECT id FROM faces LIMIT 1").fetchone()[0]
    pid = conn.execute("INSERT INTO persons (name, created_at) VALUES ('Ann','now')").lastrowid
    conn.execute("INSERT INTO face_person (face_id, person_id, similarity, status, reason, assigned_at) VALUES (?,?,?,?,?,?)",
                 (face_id, pid, 1.0, "confirmed", "", "now"))
    conn.commit(); conn.close()


def test_rescan_replaces_the_faces_of_each_photo(indexed, cli, engine):
    engine.script = [[face()], [face()], [], [], []]
    cli("faces", "--apply")
    engine.script, engine.calls = [[face(seed=1), face(x1=30, x2=50, seed=2)]] + [[]] * 4, 0
    cli("faces", "--apply", "--rescan")
    assert count(indexed.db) == 2 and count(indexed.db, "face_scans") == 5


def test_rescan_refuses_to_erase_confirmed_names(indexed, cli, engine):
    engine.script = [[face()]] * 5
    cli("faces", "--apply")
    _confirm_a_face(indexed.db)
    r = cli("faces", "--apply", "--rescan")
    assert r.code == 2 and "--discard-confirmed" in r.err
    assert scalar(indexed.db, "SELECT COUNT(*) FROM face_person WHERE status='confirmed'") == 1
    warn = cli("faces", "--rescan")                                   # a dry run only warns
    assert warn.code == 0 and "--discard-confirmed" in warn.err


def test_rescan_with_discard_confirmed_goes_ahead(indexed, cli, engine):
    engine.script = [[face()]] * 5
    cli("faces", "--apply")
    _confirm_a_face(indexed.db)
    engine.script, engine.calls = [[face(seed=7)]] * 5, 0
    r = cli("faces", "--apply", "--rescan", "--discard-confirmed")
    assert r.code == 0 and scalar(indexed.db, "SELECT COUNT(*) FROM face_person WHERE status='confirmed'") == 0


def test_photos_scanned_by_another_model_are_scanned_again(indexed, cli, engine):
    engine.script = [[face()]] * 5
    cli("faces", "--apply")
    r = cli("faces", "--apply", "--model", "buffalo_s")
    assert stat(r.out, "Photos processed:") == 5


# --------------------------------------------------------------------------- image helpers
def test_load_image_applies_exif_rotation(tmp_path):
    img = Image.new("RGB", (60, 30), (200, 10, 10))
    exif = Image.Exif(); exif[0x0112] = 6                              # rotate 90 degrees clockwise when displayed
    path = tmp_path / "r.jpg"; img.save(path, exif=exif)
    loaded, original = face_scan.load_image(str(path), 1600)
    assert loaded.size == (30, 60) and original == (30, 60)


def test_load_image_shrinks_to_max_side_but_reports_the_original_size(tmp_path):
    path = make_photo(tmp_path / "big.jpg", 1, size=(400, 200))
    loaded, original = face_scan.load_image(str(path), 100)
    assert max(loaded.size) == 100 and original == (400, 200)
    loaded, _ = face_scan.load_image(str(path), 1600)
    assert loaded.size == (400, 200)


def test_load_image_handles_png_with_alpha(tmp_path):
    path = tmp_path / "a.png"; Image.new("RGBA", (20, 20), (1, 2, 3, 128)).save(path)
    assert face_scan.load_image(str(path), 100)[0].mode == "RGB"


def test_make_thumb_is_a_square_jpeg_even_at_the_photo_edge():
    img = Image.new("RGB", (100, 100), (10, 20, 30))
    data = face_scan.make_thumb(img, (0, 0, 30, 30))
    thumb = Image.open(__import__("io").BytesIO(data))
    assert thumb.size == (face_scan.THUMB_SIZE, face_scan.THUMB_SIZE) and thumb.format == "JPEG"


def test_fmt_duration():
    assert face_scan.fmt_duration(5).endswith("s")
    assert "m" in face_scan.fmt_duration(125) and "h" in face_scan.fmt_duration(7300)


def test_iter_loaded_reports_errors_per_item_and_keeps_order(tmp_path):
    good = make_photo(tmp_path / "g.jpg", 1)
    bad = tmp_path / "b.jpg"; bad.write_bytes(b"x")
    items = [(1, str(good)), (2, str(bad)), (3, str(tmp_path / "gone.jpg")), (4, str(good))]
    out = list(face_scan.iter_loaded(items, 100, workers=2))
    assert [o[0][0] for o in out] == [1, 2, 3, 4]
    assert out[0][2] is None and out[1][2] is not None and isinstance(out[2][2], FileNotFoundError)


def test_pending_photos_rules(tmp_path):
    conn = sp.open_index(tmp_path / "x.db")
    for n in range(4):
        conn.execute("INSERT INTO photos (sha256, original_path, sorted_path, taken_at, date_source, size_bytes, sorted_at, mtime_ns) VALUES (?,?,?,?,?,?,?,?)",
                     (f"s{n}", "/o", f"/s{n}", "2020-01-01T00:00:00", "exif", 1, "now", 1))
    conn.execute("INSERT INTO face_scans VALUES (1,'now','buffalo_l',1,NULL)")
    conn.execute("INSERT INTO face_scans VALUES (2,'now','buffalo_l',0,'boom')")
    conn.execute("INSERT INTO face_scans VALUES (3,'now','other',1,NULL)")
    conn.commit()
    ids = lambda rows: [r[0] for r in rows]
    assert ids(face_scan.pending_photos(conn, "buffalo_l", False, False, True)) == [3, 4]
    assert ids(face_scan.pending_photos(conn, "buffalo_l", False, True, True)) == [2, 3, 4]
    assert ids(face_scan.pending_photos(conn, "buffalo_l", True, False, True)) == [1, 2, 3, 4]
    conn.close()


def test_pick_providers_modes(monkeypatch):
    fake = types.SimpleNamespace(get_available_providers=lambda: ["CPUExecutionProvider"])
    monkeypatch.setitem(sys.modules, "onnxruntime", fake)
    assert face_scan.pick_providers("auto") == (["CPUExecutionProvider"], False)
    assert face_scan.pick_providers("cpu") == (["CPUExecutionProvider"], False)
    with pytest.raises(RuntimeError, match="--gpu was given"):
        face_scan.pick_providers("gpu")
    fake.get_available_providers = lambda: ["CUDAExecutionProvider", "CPUExecutionProvider"]
    assert face_scan.pick_providers("auto") == (["CUDAExecutionProvider", "CPUExecutionProvider"], True)
    assert face_scan.pick_providers("cpu") == (["CPUExecutionProvider"], False)
    fake.get_available_providers = lambda: ["DmlExecutionProvider", "CPUExecutionProvider"]
    assert face_scan.pick_providers("auto")[1] is True


def test_gpucheck_without_packages_explains(env, cli, monkeypatch):
    monkeypatch.setattr(face_scan, "runtime_problem", lambda: "ImportError: nope")
    monkeypatch.setattr(face_scan, "nvidia_gpu_summary", lambda: None)
    r = cli("gpucheck", db=None)
    assert r.code == 1 and "requirements-faces.txt" in r.err
