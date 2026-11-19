"""Phase 4: the web interface where you name people and review uncertain faces.

    python sort_photos.py ui

It serves a small web app from this computer (default http://127.0.0.1:8765).
Everything stays local: the page talks only to this program, and this program
only reads and writes the index database.

What you can do in the interface
  * name a group of faces (people)
  * merge two groups that are the same person
  * confirm, correct or dismiss the faces the AI was unsure about (review queue)
  * move faces between people, or start a new person from some faces
  * see the whole photo behind any face
  * place newly scanned faces with one click (runs the phase 3 grouping)
  * sort new photos, find faces and prepare search from the Tasks page (phase 7.5, ui_tasks.py)

Choices you make here are protected from the AI: confirmed faces, named people,
merged people and ignored faces are never moved by 'cluster', even with --recluster.

Because face data is biometric, the server only answers requests for localhost,
and every change must carry a header that web pages on other sites cannot send.
"""
from __future__ import annotations

import contextlib
import io
import logging
import re
import socket
import sqlite3
import sys
import threading
import webbrowser
from datetime import datetime
from pathlib import Path

import numpy as np
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from PIL import Image, ImageDraw, ImageOps

import sort_photos as sp

log = sp.log

WEB_DIR = Path(__file__).resolve().parent / "web"
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}
MEMBER_STATUSES = ("auto", "confirmed")
MAX_FACE_IDS = 5000
SQL_CHUNK = 400
SIMILAR_MIN = 0.30        # people whose faces look at least this alike are offered as "might be the same person"
CONTEXT_MAX_SIDE = 1400

SCOPES = {
    "members": "fp.status IN ('auto', 'confirmed')",
    "review": "fp.status = 'review'",
    "unassigned": "fp.status = 'unassigned'",
    "ignored": "fp.status = 'ignored'",
}

PEOPLE_SELECT = """
    WITH member AS (
        SELECT person_id, COUNT(*) AS n, SUM(status = 'confirmed') AS c
        FROM face_person WHERE person_id IS NOT NULL AND status IN ('auto', 'confirmed') GROUP BY person_id
    ), suggested AS (
        SELECT person_id, COUNT(*) AS n FROM face_person
        WHERE person_id IS NOT NULL AND status = 'review' GROUP BY person_id
    )
    SELECT p.id, p.name, p.locked, COALESCE(m.n, 0) AS faces, COALESCE(m.c, 0) AS confirmed,
           COALESCE(s.n, 0) AS review
    FROM persons p
    LEFT JOIN member m ON m.person_id = p.id
    LEFT JOIN suggested s ON s.person_id = p.id
"""
PEOPLE_VISIBLE = "(COALESCE(m.n, 0) > 0 OR (p.name IS NOT NULL AND p.name <> ''))"  # hide empty unnamed leftovers

FACE_SELECT = """
    SELECT f.id, f.photo_id, f.face_px, fp.person_id, fp.status, fp.reason, fp.similarity,
           ph.taken_at, pe.name AS person_name
    FROM face_person fp
    JOIN faces f ON f.id = fp.face_id
    JOIN photos ph ON ph.id = f.photo_id
    LEFT JOIN persons pe ON pe.id = fp.person_id
"""


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------
def connect(db_path: Path) -> sqlite3.Connection:
    # One connection per request. FastAPI may set it up on one worker thread and run the
    # endpoint on another, but a request never uses its connection from two threads at once.
    conn = sqlite3.connect(db_path, timeout=30, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=MEMORY")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def chunks(items: list, size: int = SQL_CHUNK):
    for start in range(0, len(items), size):
        yield items[start:start + size]


def marks(count: int) -> str:
    return ",".join("?" * count)


def clean_name(value: str | None) -> str | None:
    name = re.sub(r"\s+", " ", value or "").strip()
    return name[:80] or None


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def face_json(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "photo_id": row["photo_id"],
        "person_id": row["person_id"],
        "person_name": row["person_name"],
        "status": row["status"],
        "reason": row["reason"],
        "similarity": None if row["similarity"] is None else round(row["similarity"], 2),
        "px": row["face_px"],
        "taken": (row["taken_at"] or "")[:10],
    }


def unit(vec: np.ndarray) -> np.ndarray:
    norm = float(np.linalg.norm(vec))
    return vec / norm if norm else vec


class FaceIds(BaseModel):
    face_ids: list[int] = Field(min_length=1, max_length=MAX_FACE_IDS)


class AssignBody(FaceIds):
    person_id: int | None = None
    new_person: bool = False
    name: str | None = None


class NameBody(BaseModel):
    name: str = ""
    merge_if_exists: bool = False


class MergeBody(BaseModel):
    into: int


# --------------------------------------------------------------------------
# App
# --------------------------------------------------------------------------
def create_app(db_path: Path, allowed_hosts: set[str] | None = None, any_host: bool = False,
               model_root: Path | None = None, job_command: list[str] | None = None) -> FastAPI:
    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        app.state.jobs.shutdown()  # never leave a task running after the web interface stops

    app = FastAPI(title="Photo sorter", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.db_path = db_path
    app.state.version = 0                 # bumped by every change, so cached data is refreshed
    app.state.centroids = None
    app.state.centroid_lock = threading.Lock()
    app.state.regroup_lock = threading.Lock()
    hosts = set(LOOPBACK_HOSTS) | set(allowed_hosts or ())

    @app.middleware("http")
    async def guard(request: Request, call_next):
        host = request.headers.get("host", "").lower()
        hostname = host.split("]")[0] + "]" if host.startswith("[") else host.rsplit(":", 1)[0]
        if not any_host and hostname not in hosts:
            return JSONResponse({"detail": "This address is not allowed. Use http://127.0.0.1:8765"}, status_code=403)
        if request.method not in ("GET", "HEAD") and request.headers.get("x-photo-sorter") != "1":
            return JSONResponse({"detail": "Missing request header."}, status_code=403)
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        return response

    def get_db():
        conn = connect(app.state.db_path)
        try:
            yield conn
        finally:
            conn.close()

    def changed() -> None:
        app.state.version += 1

    # ---------------------------------------------------------------- pages
    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(WEB_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    app.mount("/web", StaticFiles(directory=WEB_DIR), name="web")

    from photo_browse import add_photo_routes  # phase 5: browse the whole library
    try:
        import clip_search  # phase 7: search by description (the model itself is loaded on the first search)
        search = clip_search.PhotoSearch(model_root)
    except ImportError as error:
        log.debug("Searching by description is not available: %s", error)
        search = None
    app.state.search = search
    add_photo_routes(app, get_db, db_path.parent / "thumbs", search)

    import ui_tasks  # phase 7.5: sort, find faces and prepare search from the Tasks page

    def job_finished(job) -> None:
        app.state.centroids = None
        changed()

    app.state.jobs = ui_tasks.JobRunner(command=job_command, on_finish=job_finished)
    ui_tasks.add_task_routes(app, db_path, app.state.jobs, model_root, regroup_busy=app.state.regroup_lock.locked)

    # ----------------------------------------------------------- read: summary
    @app.get("/api/summary")
    def summary(db: sqlite3.Connection = Depends(get_db)):
        by_status = {row[0]: row[1] for row in db.execute("SELECT status, COUNT(*) FROM face_person GROUP BY status")}
        faces = db.execute("SELECT COUNT(*) FROM faces").fetchone()[0]
        grouped = sum(by_status.values())
        named = db.execute("SELECT COUNT(*) FROM persons WHERE name IS NOT NULL AND name <> ''").fetchone()[0]
        people = db.execute(f"{PEOPLE_SELECT} WHERE {PEOPLE_VISIBLE}").fetchall()
        return {
            "db_path": str(app.state.db_path),
            "photos": db.execute("SELECT COUNT(*) FROM photos").fetchone()[0],
            "faces": faces,
            "ungrouped": max(faces - grouped, 0),
            "people": len(people),
            "named": named,
            "unnamed": len(people) - named,
            "review": by_status.get("review", 0),
            "unassigned": by_status.get("unassigned", 0),
            "ignored": by_status.get("ignored", 0),
            "confirmed": by_status.get("confirmed", 0),
            "auto": by_status.get("auto", 0),
        }

    # ------------------------------------------------------------ read: people
    def person_samples(db: sqlite3.Connection, person_id: int, count: int = 4) -> list[int]:
        """A few good, varied faces of one person (for the contact strip)."""
        rows = db.execute(
            "SELECT f.id FROM face_person fp JOIN faces f ON f.id = fp.face_id"
            " WHERE fp.person_id = ? AND fp.status IN ('auto', 'confirmed')"
            " ORDER BY (fp.status = 'confirmed') DESC, f.det_score * MIN(f.face_px, 160) DESC LIMIT 40",
            (person_id,),
        ).fetchall()
        ids = [r[0] for r in rows]
        if len(ids) <= count:
            return ids
        step = len(ids) / count
        return [ids[int(i * step)] for i in range(count)]

    def people_json(db: sqlite3.Connection, rows: list[sqlite3.Row], with_samples: bool = True) -> list[dict]:
        return [
            {
                "id": r["id"], "name": r["name"] or "", "locked": bool(r["locked"]), "faces": r["faces"],
                "confirmed": r["confirmed"], "review": r["review"],
                "samples": person_samples(db, r["id"]) if with_samples else [],
            }
            for r in rows
        ]

    @app.get("/api/people")
    def list_people(q: str = "", named: str = "all", sort: str = "size", limit: int = 60, offset: int = 0,
                    db: sqlite3.Connection = Depends(get_db)):
        where, params = [PEOPLE_VISIBLE], []
        if named == "named":
            where.append("(p.name IS NOT NULL AND p.name <> '')")
        elif named == "unnamed":
            where.append("(p.name IS NULL OR p.name = '')")
        if q.strip():
            where.append("p.name LIKE ? ESCAPE '\\' COLLATE NOCASE")
            params.append("%" + q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
        order = {
            "size": "faces DESC, p.id",
            "name": "(p.name IS NULL OR p.name = ''), p.name COLLATE NOCASE, p.id",
            "review": "review DESC, faces DESC, p.id",
            "recent": "p.id DESC",
        }.get(sort, "faces DESC, p.id")
        clause = " WHERE " + " AND ".join(where)
        total = db.execute(f"SELECT COUNT(*) FROM ({PEOPLE_SELECT}{clause})", params).fetchone()[0]
        rows = db.execute(
            f"{PEOPLE_SELECT}{clause} ORDER BY {order} LIMIT ? OFFSET ?",
            [*params, max(1, min(limit, 300)), max(0, offset)],
        ).fetchall()
        return {"total": total, "people": people_json(db, rows)}

    @app.get("/api/people/{person_id:int}")
    def get_person(person_id: int, db: sqlite3.Connection = Depends(get_db)):
        rows = db.execute(f"{PEOPLE_SELECT} WHERE p.id = ?", (person_id,)).fetchall()
        if not rows:
            raise HTTPException(404, "That person no longer exists.")
        return people_json(db, rows)[0]

    # ------------------------------------------------------------- read: faces
    @app.get("/api/faces")
    def list_faces(scope: str = "members", person_id: str = "", reason: str = "", order: str = "worst",
                   limit: int = 120, offset: int = 0, db: sqlite3.Connection = Depends(get_db)):
        if scope not in SCOPES:
            raise HTTPException(400, "Unknown scope.")
        where, params = [SCOPES[scope]], []
        if person_id == "none":
            where.append("fp.person_id IS NULL")
        elif person_id:
            where.append("fp.person_id = ?")
            params.append(int(person_id))
        if reason:
            where.append("fp.reason = ?")
            params.append(reason)
        clause = " WHERE " + " AND ".join(where)
        sort = {
            "worst": "COALESCE(fp.similarity, 2) ASC, f.id",     # weakest matches first: the likeliest mistakes
            "best": "COALESCE(fp.similarity, -1) DESC, f.id",
            "date": "ph.taken_at, f.id",
        }.get(order, "f.id")
        total = db.execute(f"SELECT COUNT(*) FROM face_person fp{clause}", params).fetchone()[0]
        rows = db.execute(
            f"{FACE_SELECT}{clause} ORDER BY {sort} LIMIT ? OFFSET ?",
            [*params, max(1, min(limit, 500)), max(0, offset)],
        ).fetchall()
        return {"total": total, "faces": [face_json(r) for r in rows]}

    @app.get("/api/review/buckets")
    def review_buckets(db: sqlite3.Connection = Depends(get_db)):
        """Faces waiting for review, grouped by the person the AI suggests."""
        rows = db.execute(
            "SELECT fp.person_id, pe.name, COUNT(*) AS n FROM face_person fp"
            " LEFT JOIN persons pe ON pe.id = fp.person_id WHERE fp.status = 'review'"
            " GROUP BY fp.person_id ORDER BY (fp.person_id IS NULL), n DESC"
        ).fetchall()
        buckets = []
        for r in rows:
            samples = person_samples(db, r["person_id"], 1) if r["person_id"] is not None else []
            buckets.append({"person_id": r["person_id"], "name": r["name"] or "", "count": r["n"], "sample": samples[0] if samples else None})
        return {"buckets": buckets}

    @app.get("/api/faces/{face_id}/thumb")
    def thumb(face_id: int, db: sqlite3.Connection = Depends(get_db)):
        row = db.execute("SELECT thumb FROM faces WHERE id = ?", (face_id,)).fetchone()
        if row is None or row["thumb"] is None:
            raise HTTPException(404, "No thumbnail for this face.")
        return Response(bytes(row["thumb"]), media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})

    @app.get("/api/faces/{face_id}")
    def face_info(face_id: int, db: sqlite3.Connection = Depends(get_db)):
        row = db.execute(f"{FACE_SELECT} WHERE f.id = ?", (face_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "This face is no longer in the index.")
        photo = db.execute(
            "SELECT ph.sorted_path, ph.taken_at, (SELECT COUNT(*) FROM faces WHERE photo_id = ph.id) AS faces"
            " FROM faces f JOIN photos ph ON ph.id = f.photo_id WHERE f.id = ?", (face_id,),
        ).fetchone()
        data = face_json(row)
        data.update({"path": photo["sorted_path"], "taken_at": photo["taken_at"], "faces_in_photo": photo["faces"]})
        return data

    @app.get("/api/faces/{face_id}/photo")
    def face_photo(face_id: int, db: sqlite3.Connection = Depends(get_db)):
        row = db.execute(
            "SELECT f.x1, f.y1, f.x2, f.y2, f.photo_id, ph.sorted_path FROM faces f JOIN photos ph ON ph.id = f.photo_id"
            " WHERE f.id = ?", (face_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(404, "This face is no longer in the index.")
        others = db.execute(
            "SELECT x1, y1, x2, y2 FROM faces WHERE photo_id = ? AND id <> ?", (row["photo_id"], face_id)
        ).fetchall()
        try:
            box = (row["x1"], row["y1"], row["x2"], row["y2"])
            data = render_context(Path(row["sorted_path"]), box, [tuple(o) for o in others])
        except FileNotFoundError:
            raise HTTPException(404, f"The photo is not at {row['sorted_path']} any more. Run 'refresh' to update the index.")
        except Exception as error:  # unreadable or unsupported image
            raise HTTPException(500, f"Could not open the photo: {error}")
        return Response(data, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})

    # ------------------------------------------------- read: similar people
    def centroid_table(db: sqlite3.Connection):
        with app.state.centroid_lock:
            cached = app.state.centroids
            if cached and cached["version"] == app.state.version:
                return cached
            sums: dict[int, np.ndarray] = {}
            counts: dict[int, int] = {}
            rows = db.execute(
                "SELECT fp.person_id, f.embedding FROM face_person fp JOIN faces f ON f.id = fp.face_id"
                " WHERE fp.status IN ('auto', 'confirmed') AND fp.person_id IS NOT NULL"
            )
            for person_id, blob in rows:
                if blob is None or len(blob) != 2048:
                    continue
                vec = np.frombuffer(blob, dtype=np.float32)
                if person_id in sums:
                    sums[person_id] += vec
                else:
                    sums[person_id] = vec.copy()
                counts[person_id] = counts.get(person_id, 0) + 1
            pids = list(sums)
            matrix = np.stack([unit(sums[p]) for p in pids]) if pids else np.zeros((0, 512), dtype=np.float32)
            app.state.centroids = {"version": app.state.version, "pids": pids, "matrix": matrix, "index": {p: i for i, p in enumerate(pids)}}
            return app.state.centroids

    def similar_json(db: sqlite3.Connection, scores: list[tuple[int, float]]) -> list[dict]:
        out = []
        for person_id, score in scores:
            rows = db.execute(f"{PEOPLE_SELECT} WHERE p.id = ?", (person_id,)).fetchall()
            if rows:
                item = people_json(db, rows)[0]
                item["similarity"] = round(score, 2)
                out.append(item)
        return out

    @app.get("/api/people/{person_id:int}/similar")
    def similar_people(person_id: int, limit: int = 6, db: sqlite3.Connection = Depends(get_db)):
        table = centroid_table(db)
        i = table["index"].get(person_id)
        if i is None:
            return {"people": []}
        sims = table["matrix"] @ table["matrix"][i]
        order = np.argsort(-sims)
        scores = [(table["pids"][j], float(sims[j])) for j in order if j != i and sims[j] >= SIMILAR_MIN][:limit]
        return {"people": similar_json(db, scores)}

    @app.post("/api/faces/suggest")
    def suggest_people(body: FaceIds, db: sqlite3.Connection = Depends(get_db)):
        """Which existing people do these faces look most like?"""
        table = centroid_table(db)
        vectors = []
        for part in chunks(body.face_ids):
            for (blob,) in db.execute(f"SELECT embedding FROM faces WHERE id IN ({marks(len(part))})", part):
                if blob is not None and len(blob) == 2048:
                    vectors.append(np.frombuffer(blob, dtype=np.float32))
        if not vectors or not len(table["pids"]):
            return {"people": []}
        sims = table["matrix"] @ unit(np.mean(vectors, axis=0))
        order = np.argsort(-sims)[:6]
        return {"people": similar_json(db, [(table["pids"][j], float(sims[j])) for j in order if sims[j] >= SIMILAR_MIN])}

    # ------------------------------------------------------------ write: people
    def get_existing_person(db: sqlite3.Connection, person_id: int) -> sqlite3.Row:
        row = db.execute("SELECT id, name, locked FROM persons WHERE id = ?", (person_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "That person no longer exists. Reload the page.")
        return row

    def merge_people(db: sqlite3.Connection, source: int, target: int) -> None:
        src, dst = get_existing_person(db, source), get_existing_person(db, target)
        if source == target:
            raise HTTPException(400, "Choose a different person to merge into.")
        with db:
            db.execute("UPDATE face_person SET person_id = ? WHERE person_id = ?", (target, source))
            db.execute(
                "DELETE FROM face_rejections WHERE person_id = ? AND face_id IN (SELECT face_id FROM face_person WHERE person_id = ?)",
                (target, target),
            )
            if not (dst["name"] or "").strip() and (src["name"] or "").strip():
                db.execute("UPDATE persons SET name = ? WHERE id = ?", (src["name"], target))
            db.execute("UPDATE persons SET locked = 1 WHERE id = ?", (target,))
            db.execute("DELETE FROM persons WHERE id = ?", (source,))
        changed()

    @app.post("/api/people/{person_id:int}/name")
    def rename_person(person_id: int, body: NameBody, db: sqlite3.Connection = Depends(get_db)):
        get_existing_person(db, person_id)
        name = clean_name(body.name)
        if name:
            other = db.execute(
                "SELECT id, name FROM persons WHERE id <> ? AND name = ? COLLATE NOCASE", (person_id, name)
            ).fetchone()
            if other:
                if not body.merge_if_exists:
                    return JSONResponse(
                        {"detail": f"{other['name']} already exists.", "conflict_id": other["id"], "conflict_name": other["name"]},
                        status_code=409,
                    )
                merge_people(db, person_id, other["id"])
                return {"merged_into": other["id"], "name": other["name"]}
        with db:
            db.execute("UPDATE persons SET name = ?, locked = 1 WHERE id = ?", (name, person_id))
        changed()
        return {"id": person_id, "name": name or ""}

    @app.post("/api/people/{person_id:int}/merge")
    def merge_person(person_id: int, body: MergeBody, db: sqlite3.Connection = Depends(get_db)):
        merge_people(db, person_id, body.into)
        return {"merged_into": body.into}

    # ------------------------------------------------------------- write: faces
    def existing_face_ids(db: sqlite3.Connection, ids: list[int]) -> list[int]:
        found: list[int] = []
        for part in chunks(sorted(set(ids))):
            found += [r[0] for r in db.execute(f"SELECT id FROM faces WHERE id IN ({marks(len(part))})", part)]
        return found

    def current_rows(db: sqlite3.Connection, ids: list[int]) -> dict[int, tuple[int | None, str]]:
        rows: dict[int, tuple[int | None, str]] = {}
        for part in chunks(ids):
            for r in db.execute(f"SELECT face_id, person_id, status FROM face_person WHERE face_id IN ({marks(len(part))})", part):
                rows[r["face_id"]] = (r["person_id"], r["status"])
        return rows

    def drop_empty_people(db: sqlite3.Connection, person_ids: set[int]) -> None:
        """Remove unnamed, unprotected people that no longer have any face rows."""
        for person_id in person_ids:
            db.execute(
                "DELETE FROM persons WHERE id = ? AND locked = 0 AND (name IS NULL OR name = '')"
                " AND NOT EXISTS (SELECT 1 FROM face_person WHERE person_id = persons.id)",
                (person_id,),
            )

    @app.post("/api/faces/confirm")
    def confirm_faces(body: FaceIds, db: sqlite3.Connection = Depends(get_db)):
        """Accept the AI's suggestion (or its automatic match) for these faces."""
        ids = existing_face_ids(db, body.face_ids)
        rows = current_rows(db, ids)
        done, skipped = 0, 0
        stamp = now()
        with db:
            for face_id in ids:
                person_id, status = rows.get(face_id, (None, ""))
                if person_id is None or status in ("ignored", "unassigned"):
                    skipped += 1
                    continue
                db.execute(
                    "UPDATE face_person SET status = 'confirmed', reason = NULL, assigned_at = ? WHERE face_id = ?", (stamp, face_id)
                )
                db.execute("DELETE FROM face_rejections WHERE face_id = ? AND person_id = ?", (face_id, person_id))
                db.execute("UPDATE persons SET locked = 1 WHERE id = ?", (person_id,))
                done += 1
        changed()
        return {"changed": done, "skipped": skipped}

    @app.post("/api/faces/assign")
    def assign_faces(body: AssignBody, db: sqlite3.Connection = Depends(get_db)):
        """Put faces in a person (existing, or a new one) and mark them as confirmed by you."""
        if not body.new_person and body.person_id is None:
            raise HTTPException(400, "Choose a person.")
        ids = existing_face_ids(db, body.face_ids)
        rows = current_rows(db, ids)
        stamp = now()
        left: set[int] = set()
        with db:
            if body.new_person:
                target = db.execute(
                    "INSERT INTO persons (name, created_at, locked) VALUES (?, ?, 1)", (clean_name(body.name), stamp)
                ).lastrowid
            else:
                target = get_existing_person(db, body.person_id)["id"]
                db.execute("UPDATE persons SET locked = 1 WHERE id = ?", (target,))
            moved = 0
            for face_id in ids:
                old, _ = rows.get(face_id, (None, ""))
                if old is not None and old != target:
                    db.execute("INSERT OR IGNORE INTO face_rejections (face_id, person_id) VALUES (?, ?)", (face_id, old))
                    left.add(old)
                db.execute("DELETE FROM face_rejections WHERE face_id = ? AND person_id = ?", (face_id, target))
                db.execute(
                    "INSERT OR REPLACE INTO face_person (face_id, person_id, similarity, status, reason, assigned_at)"
                    " VALUES (?, ?, NULL, 'confirmed', NULL, ?)", (face_id, target, stamp),
                )
                moved += 1
            drop_empty_people(db, left)
        changed()
        return {"changed": moved, "person_id": target}

    @app.post("/api/faces/reject")
    def reject_faces(body: FaceIds, db: sqlite3.Connection = Depends(get_db)):
        """'This is not that person': remember it and send the face back to the review queue."""
        ids = existing_face_ids(db, body.face_ids)
        rows = current_rows(db, ids)
        stamp = now()
        left: set[int] = set()
        done = 0
        with db:
            for face_id in ids:
                person_id, _ = rows.get(face_id, (None, ""))
                if person_id is None:
                    continue
                db.execute("INSERT OR IGNORE INTO face_rejections (face_id, person_id) VALUES (?, ?)", (face_id, person_id))
                db.execute(
                    "UPDATE face_person SET person_id = NULL, status = 'review', reason = 'rejected', similarity = NULL,"
                    " assigned_at = ? WHERE face_id = ?", (stamp, face_id),
                )
                left.add(person_id)
                done += 1
            drop_empty_people(db, left)
        changed()
        return {"changed": done}

    @app.post("/api/faces/ignore")
    def ignore_faces(body: FaceIds, db: sqlite3.Connection = Depends(get_db)):
        """Dismiss faces (strangers, not a real face). The AI never places them again."""
        ids = existing_face_ids(db, body.face_ids)
        rows = current_rows(db, ids)
        stamp = now()
        left = {p for p, _ in rows.values() if p is not None}
        with db:
            for face_id in ids:
                db.execute(
                    "INSERT OR REPLACE INTO face_person (face_id, person_id, similarity, status, reason, assigned_at)"
                    " VALUES (?, NULL, NULL, 'ignored', NULL, ?)", (face_id, stamp),
                )
            drop_empty_people(db, left)
        changed()
        return {"changed": len(ids)}

    @app.post("/api/faces/restore")
    def restore_faces(body: FaceIds, db: sqlite3.Connection = Depends(get_db)):
        """Bring ignored faces back into the review queue."""
        ids = existing_face_ids(db, body.face_ids)
        stamp = now()
        with db:
            for face_id in ids:
                db.execute(
                    "UPDATE face_person SET status = 'review', reason = 'restored', assigned_at = ?"
                    " WHERE face_id = ? AND status = 'ignored'", (stamp, face_id),
                )
        changed()
        return {"changed": len(ids)}

    # ---------------------------------------------------------------- regroup
    @app.post("/api/regroup")
    def regroup():
        """Run the phase 3 grouping for faces that are new or still uncertain."""
        if app.state.jobs.busy():
            raise HTTPException(409, f"'{app.state.jobs.job.title}' is running on the Tasks page. Try again when it has finished.")
        if not app.state.regroup_lock.acquire(blocking=False):
            raise HTTPException(409, "Grouping is already running.")
        lines: list[str] = []

        class Collect(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                lines.append(record.getMessage())

        handler = Collect(level=logging.INFO)
        previous_level = log.level
        if log.getEffectiveLevel() > logging.INFO:  # make sure the progress lines are produced
            log.setLevel(logging.INFO)
        log.addHandler(handler)
        try:
            import face_cluster
            args = sp.build_parser().parse_args(["cluster", "--apply", "--db", str(app.state.db_path)])
            code = face_cluster.cluster_faces(args)
        except ImportError as error:
            raise HTTPException(500, f"Grouping needs numpy: {error}")
        finally:
            log.removeHandler(handler)
            log.setLevel(previous_level)
            app.state.regroup_lock.release()
        changed()
        return {"ok": code == 0, "lines": [line for line in lines if line.strip()]}

    return app


# --------------------------------------------------------------------------
# Photo with the face outlined
# --------------------------------------------------------------------------
def render_context(path: Path, box: tuple, others: list[tuple]) -> bytes:
    with Image.open(path) as opened:
        image = ImageOps.exif_transpose(opened).convert("RGB")
    image.thumbnail((CONTEXT_MAX_SIDE, CONTEXT_MAX_SIDE))
    width, height = image.size
    draw = ImageDraw.Draw(image)

    def rect(b: tuple, colour: tuple, thickness: int) -> None:
        x1, y1, x2, y2 = b[0] * width, b[1] * height, b[2] * width, b[3] * height
        for offset in range(thickness):
            draw.rectangle([x1 - offset, y1 - offset, x2 + offset, y2 + offset], outline=colour)

    for other in others:
        rect(other, (255, 255, 255), 1)
    rect(box, (0, 0, 0), 5)           # dark outline under the bright one keeps it visible on any photo
    rect(box, (255, 214, 0), 3)
    out = io.BytesIO()
    image.save(out, "JPEG", quality=86)
    return out.getvalue()


# --------------------------------------------------------------------------
# Command
# --------------------------------------------------------------------------
def run_ui(args) -> int:
    db_path = (args.db or sp.default_db_path()).resolve()
    sp.log_index_location(db_path, True)
    if not db_path.exists():
        log.info("There is no index yet, so an empty one is made. Sort your first photos on the Tasks page.")
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        sp.open_index(db_path).close()  # make sure the newest tables exist
    except (OSError, sqlite3.Error) as error:
        log.error("Cannot open the index %s: %s", db_path, error)
        return 2
    try:
        import uvicorn
    except ImportError as error:
        log.error("The web interface needs uvicorn (%s). Install it with: pip install -r requirements-ui.txt", error)
        return 2

    loopback = args.host in LOOPBACK_HOSTS
    if not loopback:
        log.warning(
            "Listening on %s: anyone who can reach this computer on the network can see and change your face data. "
            "There is no password. Use the default 127.0.0.1 unless you know what you are doing.", args.host,
        )
    probe = socket.socket(socket.AF_INET6 if ":" in args.host else socket.AF_INET, socket.SOCK_STREAM)
    if sys.platform != "win32":  # same as uvicorn: a port in TIME_WAIT after a restart is not "in use"
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        probe.bind((args.host, args.port))
    except OSError as error:
        log.error("Cannot use port %d (%s). Choose another with --port, for example --port %d.", args.port, error, args.port + 1)
        return 2
    finally:
        probe.close()

    app = create_app(db_path, allowed_hosts={args.host}, any_host=not loopback and args.host in ("0.0.0.0", "::"))
    shown = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    url = f"http://{shown}:{args.port}"
    log.info("Web interface: %s  (press Ctrl+C to stop)", url)
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    except KeyboardInterrupt:
        pass
    log.info("Web interface stopped.")
    return 0
