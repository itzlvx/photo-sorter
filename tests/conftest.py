"""Shared test tools: photo makers, a CLI runner, and a synthetic face index with known identities."""
from __future__ import annotations

import argparse
import io
import os
import shutil
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import sort_photos as sp  # noqa: E402

SIGMA = 0.816   # makes two faces of the same person ~0.60 similar, and a face ~0.775 similar to its centre


# --------------------------------------------------------------------------- photos
def make_photo(path: Path, seed: int = 0, size=(64, 48), taken=None, digitized=None, modified=None,
               mtime=None, fmt="JPEG") -> Path:
    """Write a small image whose content depends on `seed` (different seeds -> different hashes).

    taken / digitized / modified are datetimes or raw EXIF strings; mtime sets the file's modified time.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    pixels = np.random.default_rng(seed).integers(0, 255, (size[1], size[0], 3), dtype=np.uint8)
    img = Image.fromarray(pixels)
    exif = Image.Exif()

    def text(value):
        return value.strftime("%Y:%m:%d %H:%M:%S") if isinstance(value, datetime) else value

    ifd = exif.get_ifd(sp.EXIF_IFD)
    if taken is not None:
        ifd[sp.TAG_DATETIME_ORIGINAL] = text(taken)
    if digitized is not None:
        ifd[sp.TAG_DATETIME_DIGITIZED] = text(digitized)
    if modified is not None:
        exif[sp.TAG_DATETIME] = text(modified)
    kwargs = {"exif": exif} if fmt == "JPEG" and (taken or digitized or modified) else {}
    img.save(path, fmt, **kwargs)
    if mtime is not None:
        stamp = mtime.timestamp() if isinstance(mtime, datetime) else mtime
        os.utime(path, (stamp, stamp))
    return path


def stat(text: str, label: str) -> int:
    """Read a number from a summary line such as 'Photos sorted:   3' (whatever the spacing)."""
    import re
    match = re.search(re.escape(label) + r"\s+(\d+)", text)
    assert match, f"no '{label}' line in:\n{text}"
    return int(match.group(1))


def tree(root: Path) -> list[str]:
    """Every file under root as sorted posix-style relative paths (the trash folder included)."""
    return sorted(p.relative_to(root).as_posix() for p in Path(root).rglob("*") if p.is_file())


# --------------------------------------------------------------------------- environment and CLI
@dataclass
class Env:
    tmp: Path
    src: Path
    dest: Path
    db: Path


@pytest.fixture(autouse=True)
def isolated_machine(tmp_path, monkeypatch):
    """Tests must never touch the real index or settings of the person running them."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))


@pytest.fixture
def env(tmp_path) -> Env:
    src = tmp_path / "incoming"
    src.mkdir()
    return Env(tmp=tmp_path, src=src, dest=tmp_path / "library", db=tmp_path / "index" / "photo_index.db")


@dataclass
class Result:
    code: int
    out: str
    err: str

    @property
    def text(self) -> str:
        return self.out + self.err


@pytest.fixture
def cli(capsys, env):
    """cli("sort", src, dest, "--apply") -> Result. Adds --db automatically (pass db=None to skip)."""
    def run(*argv, db=True) -> Result:
        words = [str(a) for a in argv]
        if db is True and words and words[0] not in {"-h", "--help"}:
            words += ["--db", str(env.db)]
        elif db not in (True, None, False):
            words += ["--db", str(db)]
        code = sp.main(words)
        captured = capsys.readouterr()
        return Result(code, captured.out, captured.err)
    return run


def connect(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def scalar(db: Path, sql: str, *params):
    conn = connect(db)
    try:
        return conn.execute(sql, params).fetchone()[0]
    finally:
        conn.close()


# --------------------------------------------------------------------------- synthetic faces
def unit(vector):
    return vector / np.linalg.norm(vector)


def jpeg_bytes(color=(120, 120, 140), size=(48, 48)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, "JPEG", quality=70)
    return out.getvalue()


class FaceIndex:
    """Builds an index with known identities: person k has centre C[k], faces are noisy copies."""

    def __init__(self, db: Path, library: Path | None = None, seed: int = 11, files: bool = True):
        self.db = Path(db)
        self.db.parent.mkdir(parents=True, exist_ok=True)
        self.library = Path(library) if library else self.db.parent / "library"
        self.files = files
        self.rng = np.random.default_rng(seed)
        self.conn = sp.open_index(self.db)
        self.n = 0
        self.centres: list[np.ndarray] = []
        self.faces: dict[int, list[int]] = {}      # identity -> face ids
        self.photo_of: dict[int, int] = {}         # face id -> photo id

    def centre(self, k: int) -> np.ndarray:
        while len(self.centres) <= k:
            self.centres.append(unit(self.rng.standard_normal(512)))
        return self.centres[k]

    def add_photo(self, month: int = 1, year: int = 2020, scanned: int | None = None) -> int:
        self.n += 1
        folder = self.library / f"{year}" / f"{month:02d}-{sp.MONTH_ABBR[month - 1]}"
        path = folder / f"img{self.n:04d}.jpg"
        if self.files:
            make_photo(path, seed=1000 + self.n, size=(96, 72))
        row = self.conn.execute(
            "INSERT INTO photos (sha256, original_path, sorted_path, taken_at, date_source, size_bytes, sorted_at, mtime_ns)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (f"sha{self.n:06d}", str(path), str(path), f"{year}-{month:02d}-15T10:00:00", "exif_original", 1, "now", 1),
        )
        photo_id = row.lastrowid
        if scanned is not None:
            self.conn.execute("INSERT INTO face_scans (photo_id, scanned_at, model, face_count, error) VALUES (?,?,?,?,NULL)",
                              (photo_id, "now", "buffalo_l", scanned))
        return photo_id

    def add_face(self, identity: int | None, photo_id: int | None = None, sigma: float = SIGMA, det=0.9, px=120,
                 blur=100.0, yaw=5.0, model="buffalo_l", box=(0.4, 0.25, 0.6, 0.65)) -> int:
        photo_id = photo_id or self.add_photo(month=1 + self.n % 12, year=2018 + self.n % 5, scanned=1)
        if identity is None:
            vector = unit(self.rng.standard_normal(512))
        else:
            vector = unit(self.centre(identity) + sigma * unit(self.rng.standard_normal(512)))
        face_id = self.conn.execute(
            "INSERT INTO faces (photo_id,x1,y1,x2,y2,det_score,face_px,blur,yaw,embedding,thumb,model) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (photo_id, *box, det, px, blur, yaw, vector.astype(np.float32).tobytes(), jpeg_bytes(), model),
        ).lastrowid
        self.faces.setdefault(identity if identity is not None else -1, []).append(face_id)
        self.photo_of[face_id] = photo_id
        return face_id

    def people(self, *sizes: int, **kw) -> "FaceIndex":
        for identity, size in enumerate(sizes):
            for _ in range(size):
                self.add_face(identity, **kw)
        return self

    def commit(self) -> "FaceIndex":
        self.conn.commit()
        return self

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()


@pytest.fixture
def face_index(env):
    """face_index(sizes..., strangers=0, photos=True) -> FaceIndex (committed, still open until test ends)."""
    made: list[FaceIndex] = []

    def build(*sizes: int, strangers: int = 0, files: bool = True, **kw) -> FaceIndex:
        index = FaceIndex(env.db, env.dest, files=files)
        index.people(*sizes, **kw)
        for _ in range(strangers):
            index.add_face(None)
        index.commit()
        made.append(index)
        return index

    yield build
    for index in made:
        try:
            index.close()
        except sqlite3.Error:
            pass


def cluster_args(**over) -> argparse.Namespace:
    values = dict(apply=False, recluster=False, threshold=0.50, review_threshold=0.35, merge_threshold=0.60, margin=0.06,
                  min_group_size=3, min_det_score=0.60, min_face_px=50, min_blur=20.0, max_yaw=45.0, model=None, db=None)
    values.update(over)
    return argparse.Namespace(**values)


def draw_face_photo(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (400, 300), (170, 190, 170))
    ImageDraw.Draw(img).ellipse([150, 60, 250, 200], fill=(235, 200, 170))
    img.save(path, "JPEG")
    return path


# --------------------------------------------------------------------------- web interface
class UI:
    """A test client for the web interface plus the synthetic index behind it."""

    def __init__(self, app, client, index, env):
        self.app, self.client, self.index, self.env = app, client, index, env
        self.db = env.db

    def get(self, path, **kw):
        return self.client.get(path, **kw)

    def post(self, path, **body):
        return self.client.post(path, json=body, headers={"X-Photo-Sorter": "1"})

    def ok(self, response):
        assert response.status_code == 200, (response.status_code, response.text)
        return response.json()

    def j(self, path):
        return self.ok(self.get(path))

    def people(self):
        return self.j("/api/people?limit=300")["people"]

    def faces(self, **params):
        query = "&".join(f"{k}={v}" for k, v in params.items())
        return self.j(f"/api/faces?limit=500&{query}")["faces"]

    def summary(self):
        return self.j("/api/summary")


@pytest.fixture
def ui(env, face_index):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    import face_cluster
    import ui_server
    from fastapi.testclient import TestClient

    index = face_index(30, 20, 12, strangers=6)
    for _ in range(8):
        index.add_face(0, px=30, sigma=1.5)          # tiny noisy faces of person 0 -> land in the review queue
    index.commit()
    assert face_cluster.cluster_faces(cluster_args(db=env.db, apply=True)) == 0
    app = ui_server.create_app(env.db)
    return UI(app, TestClient(app, base_url="http://127.0.0.1:8765"), index, env)
