"""Phase 5: browse the whole library, with quick filters for recognised people.

Adds these routes to the web interface (see ui_server.py):
  GET /api/photos                 the photo list, with filters
  GET /api/photos/{id}            one photo: where it is, when it was taken, and every face in it
  GET /api/photos/{id}/thumb      small picture for the grid (made on first use, then kept on this computer)
  GET /api/photos/{id}/image      larger picture for the viewer
  GET /api/library                years and months with counts, and the counts for the quick filters
  GET /api/people/chips           named people with how many photos they are in (the quick links)
  GET /api/search/models          models with search vectors (phase 7); /api/photos?d=... searches by description
  POST /api/photos/{id}/note      save your note on a photo (phase 6; searchable from the Photos page)
  POST /api/photos/{id}/rename    give the file a new name (same folder, same extension)
  POST /api/photos/delete         delete several photos at once (same as below, one by one)
  POST /api/photos/{id}/delete    move the file to a '.photo-sorter-trash' folder and drop it from the index

Thumbnails are cached in a 'thumbs' folder next to the index database (on this computer, not on the
network drive that holds your photos), named by the photo's content hash so they survive renames.
Opening photos over a network drive can be slow the first time; after that the grid is fast.
"""
from __future__ import annotations

import os
import re
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
import numpy as np
from PIL import Image, ImageOps
from pydantic import BaseModel

import photo_meta
import sort_photos as sp  # also registers HEIC support

SEARCH_UNAVAILABLE = ("Searching by description needs numpy, Pillow and tokenizers. Install them with: "
                      "pip install -r requirements-search.txt, then restart the web interface.")
BAD_NAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
MAX_NAME = 150


class NameBody(BaseModel):
    name: str


class NoteBody(BaseModel):
    text: str = ""


class PhotoIds(BaseModel):
    photo_ids: list[int]


MAX_BULK = 500


def clean_file_name(typed: str, suffix: str) -> str:
    """Turn what the person typed into a safe Windows file name that keeps the original extension."""
    base = typed.strip()
    if base.lower().endswith(suffix.lower()):
        base = base[: len(base) - len(suffix)]
    base = base.strip()
    if not base.strip("."):
        raise HTTPException(400, "Type a name for the file.")
    if BAD_NAME_CHARS.search(base):
        raise HTTPException(400, 'A file name can\'t contain these characters: \\ / : * ? " < > |')
    if base.endswith("."):
        raise HTTPException(400, "A file name can't end with a full stop.")
    if base.split(".")[0].upper() in RESERVED:
        raise HTTPException(400, "Windows doesn't allow that name. Try another.")
    if len(base) > MAX_NAME:
        raise HTTPException(400, f"That name is too long (the limit is {MAX_NAME} characters).")
    return base + suffix


def library_root(path: Path) -> Path:
    """Where the trash folder lives: the library folder when the file sits in Year/NN-mon, else its own folder."""
    month, year = path.parent, path.parent.parent
    if re.fullmatch(r"\d{2}(-[a-z]+)?|[A-Za-z]+", month.name) and re.fullmatch(r"\d{4}", year.name):
        return year.parent
    return path.parent


THUMB_SIDE = 360
VIEW_SIDE_DEFAULT = 1600
SQL_CHUNK = 400
DECODE_SLOTS = threading.BoundedSemaphore(4)   # decoding big photos is heavy; do a few at a time
CONFIDENT = "('auto', 'confirmed')"
WITH_UNCERTAIN = "('auto', 'confirmed', 'review')"

# Conditions on the photo table (alias ph) for the quick filters.
FILTERS = {
    # at least one face that is not tied to a named person (and was not ignored)
    "unlabeled": (
        "EXISTS (SELECT 1 FROM faces f LEFT JOIN face_person fp ON fp.face_id = f.id"
        " LEFT JOIN persons pe ON pe.id = fp.person_id WHERE f.photo_id = ph.id AND COALESCE(fp.status, '') <> 'ignored'"
        " AND (fp.face_id IS NULL OR fp.status IN ('review', 'unassigned') OR pe.name IS NULL OR pe.name = ''))"
    ),
    "review": (
        "EXISTS (SELECT 1 FROM faces f JOIN face_person fp ON fp.face_id = f.id"
        " WHERE f.photo_id = ph.id AND fp.status = 'review')"
    ),
    "noface": "EXISTS (SELECT 1 FROM face_scans s WHERE s.photo_id = ph.id AND s.face_count = 0 AND s.error IS NULL)",
    "unscanned": "NOT EXISTS (SELECT 1 FROM face_scans s WHERE s.photo_id = ph.id)",
    # phase 6: photos you wrote a note on
    "notes": f"EXISTS (SELECT 1 FROM photo_annotations a WHERE a.photo_id = ph.id AND a.kind = 'note' AND a.source = '{photo_meta.YOU}')",
}


def marks(count: int) -> str:
    return ",".join("?" * count)


def file_name(path: str) -> str:
    return re.split(r"[\\/]", path)[-1]


def render_jpeg(path: Path, side: int, quality: int) -> bytes:
    """Upright (EXIF-rotated) JPEG whose longest side is at most `side`."""
    import io
    with DECODE_SLOTS:
        with Image.open(path) as opened:
            opened.draft("RGB", (side, side))       # JPEGs: decode at reduced size, much faster
            image = ImageOps.exif_transpose(opened).convert("RGB")
        image.thumbnail((side, side), Image.LANCZOS)
        out = io.BytesIO()
        image.save(out, "JPEG", quality=quality)
        return out.getvalue()


def add_photo_routes(app: FastAPI, get_db, thumb_dir: Path, search=None) -> None:
    """search: a clip_search.PhotoSearch for searching by description (phase 7), or None when it cannot be loaded."""
    def photo_row(db: sqlite3.Connection, photo_id: int) -> sqlite3.Row:
        row = db.execute("SELECT id, sha256, sorted_path, taken_at, date_source, size_bytes FROM photos WHERE id = ?", (photo_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "This photo is no longer in the index.")
        return row

    def missing(path: str) -> HTTPException:
        return HTTPException(404, f"The photo is not at {path} any more. Run 'refresh' to update the index.")

    # ----------------------------------------------------------------- pictures
    @app.get("/api/photos/{photo_id}/thumb")
    def photo_thumb(photo_id: int, db: sqlite3.Connection = Depends(get_db)):
        row = photo_row(db, photo_id)
        cached = thumb_dir / row["sha256"][:2] / f"{row['sha256']}_{THUMB_SIDE}.jpg"
        headers = {"Cache-Control": "private, max-age=604800"}
        if cached.exists():
            return FileResponse(cached, media_type="image/jpeg", headers=headers)
        try:
            data = render_jpeg(Path(row["sorted_path"]), THUMB_SIDE, 80)
        except FileNotFoundError:
            raise missing(row["sorted_path"])
        except Exception as error:
            raise HTTPException(500, f"Could not open the photo: {error}")
        try:  # keep it for next time; a failure to cache must not break the page
            cached.parent.mkdir(parents=True, exist_ok=True)
            temporary = cached.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
            temporary.write_bytes(data)
            os.replace(temporary, cached)
        except OSError:
            pass
        return Response(data, media_type="image/jpeg", headers=headers)

    @app.get("/api/photos/{photo_id}/image")
    def photo_image(photo_id: int, side: int = VIEW_SIDE_DEFAULT, db: sqlite3.Connection = Depends(get_db)):
        row = photo_row(db, photo_id)
        try:
            data = render_jpeg(Path(row["sorted_path"]), max(600, min(side, 3000)), 86)
        except FileNotFoundError:
            raise missing(row["sorted_path"])
        except Exception as error:
            raise HTTPException(500, f"Could not open the photo: {error}")
        return Response(data, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})

    # -------------------------------------------------------------------- detail
    @app.get("/api/photos/{photo_id}")
    def photo_detail(photo_id: int, db: sqlite3.Connection = Depends(get_db)):
        row = photo_row(db, photo_id)
        faces = db.execute(
            "SELECT f.id, f.x1, f.y1, f.x2, f.y2, fp.status, fp.person_id, pe.name FROM faces f"
            " LEFT JOIN face_person fp ON fp.face_id = f.id LEFT JOIN persons pe ON pe.id = fp.person_id"
            " WHERE f.photo_id = ? ORDER BY f.x1", (photo_id,),
        ).fetchall()
        scanned = db.execute("SELECT 1 FROM face_scans WHERE photo_id = ?", (photo_id,)).fetchone() is not None
        camera = photo_meta.camera_json(db.execute("SELECT * FROM photo_exif WHERE photo_id = ?", (photo_id,)).fetchone())
        annotations = photo_meta.annotations_for(db, photo_id)
        note = next((a["text"] for a in annotations if a["kind"] == "note" and a["source"] == photo_meta.YOU), "")
        return {
            "id": row["id"], "path": row["sorted_path"], "name": file_name(row["sorted_path"]),
            "taken_at": row["taken_at"], "date_source": row["date_source"], "size_bytes": row["size_bytes"],
            "scanned": scanned,
            "camera": camera,
            "note": note,
            "annotations": [a for a in annotations if not (a["kind"] == "note" and a["source"] == photo_meta.YOU)],
            "faces": [
                {"id": f["id"], "box": [f["x1"], f["y1"], f["x2"], f["y2"]], "status": f["status"] or "",
                 "person_id": f["person_id"], "name": f["name"] or ""}
                for f in faces
            ],
        }

    # ---------------------------------------------------------------------- note
    @app.post("/api/photos/{photo_id}/note")
    def save_note(photo_id: int, body: NoteBody, db: sqlite3.Connection = Depends(get_db)):
        photo_row(db, photo_id)
        try:
            with db:
                text = photo_meta.set_note(db, photo_id, body.text)
        except ValueError as error:
            raise HTTPException(400, str(error))
        return {"id": photo_id, "note": text}

    # ------------------------------------------------------------ rename and delete
    @app.post("/api/photos/{photo_id}/rename")
    def rename_photo(photo_id: int, body: NameBody, db: sqlite3.Connection = Depends(get_db)):
        row = photo_row(db, photo_id)
        old = Path(row["sorted_path"])
        if not old.exists():
            raise missing(row["sorted_path"])
        new = old.with_name(clean_file_name(body.name, old.suffix))
        if new.name == old.name:
            return {"id": photo_id, "name": new.name, "path": str(new), "changed": False}
        same_file_new_case = new.name.lower() == old.name.lower()   # Report.JPG -> report.jpg is allowed
        if new.exists() and not same_file_new_case:
            raise HTTPException(409, f"There is already a file called {new.name} in that folder. Choose another name.")
        if db.execute("SELECT 1 FROM photos WHERE sorted_path = ? AND id <> ?", (str(new), photo_id)).fetchone() and not same_file_new_case:
            raise HTTPException(409, f"{new.name} is already used by another photo in the index. Choose another name.")
        try:
            os.rename(old, new)
        except OSError as error:
            raise HTTPException(500, f"Windows would not rename the file: {error}")
        try:
            db.execute("UPDATE photos SET sorted_path = ? WHERE id = ?", (str(new), photo_id))
            db.commit()
        except sqlite3.Error as error:
            try:
                os.rename(new, old)   # keep the disk and the index in agreement
            except OSError:
                pass
            raise HTTPException(500, f"The file was not renamed: {error}")
        return {"id": photo_id, "name": new.name, "path": str(new), "changed": True}

    def delete_one(db: sqlite3.Connection, photo_id: int) -> dict:
        """Move the file into <library>/.photo-sorter-trash (never erased) and forget it, including its faces."""
        row = photo_row(db, photo_id)
        path = Path(row["sorted_path"])
        moved_to = None
        if path.exists():
            trash = library_root(path) / sp.TRASH_DIR
            try:
                trash.mkdir(exist_ok=True)
                target = trash / path.name
                counter = 1
                while target.exists():
                    counter += 1
                    target = trash / f"{path.stem} ({counter}){path.suffix}"
                os.rename(path, target)
                moved_to = target
                with open(trash / "deleted.log", "a", encoding="utf-8") as log_file:
                    log_file.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}\t{target.name}\twas {path}\n")
            except OSError as error:
                raise HTTPException(500, f"{path.name} was not deleted, because it could not be moved to the trash folder: {error}")
        people = {r[0] for r in db.execute(
            "SELECT DISTINCT fp.person_id FROM face_person fp JOIN faces f ON f.id = fp.face_id"
            " WHERE f.photo_id = ? AND fp.person_id IS NOT NULL", (photo_id,))}
        db.execute("DELETE FROM photos WHERE id = ?", (photo_id,))          # faces and their matches go with it
        for person_id in people:                                            # unnamed groups left with no faces
            db.execute(
                "DELETE FROM persons WHERE id = ? AND locked = 0 AND (name IS NULL OR name = '')"
                " AND NOT EXISTS (SELECT 1 FROM face_person WHERE person_id = persons.id)", (person_id,))
        db.commit()
        return {"id": photo_id, "was_missing": moved_to is None, "trash": str(moved_to) if moved_to else None}

    @app.post("/api/photos/{photo_id}/delete")
    def delete_photo(photo_id: int, db: sqlite3.Connection = Depends(get_db)):
        return delete_one(db, photo_id)

    @app.post("/api/photos/delete")
    def delete_photos(body: PhotoIds, db: sqlite3.Connection = Depends(get_db)):
        """Delete several photos. Each one is handled on its own, so one failure does not stop the rest."""
        ids = list(dict.fromkeys(body.photo_ids))
        if not ids:
            raise HTTPException(400, "No photos were chosen.")
        if len(ids) > MAX_BULK:
            raise HTTPException(400, f"Choose at most {MAX_BULK} photos at a time.")
        deleted, failed = [], []
        for photo_id in ids:
            try:
                delete_one(db, photo_id)
                deleted.append(photo_id)
            except HTTPException as error:
                if error.status_code == 404:
                    deleted.append(photo_id)       # already gone from the index: nothing left to do
                else:
                    failed.append({"id": photo_id, "error": error.detail})
        return {"deleted": deleted, "failed": failed}

    # ---------------------------------------------------------------------- list
    @app.get("/api/search/models")
    def search_models(db: sqlite3.Connection = Depends(get_db)):
        """Models with search vectors in the index (phase 7), the one used by default, and the result limits."""
        if search is None:
            return {"models": [], "default": "", "limits": [], "unavailable": SEARCH_UNAVAILABLE}
        models = search.stored_models(db)
        ready = [m for m in models if m["ready"]]
        return {"models": models, "default": (ready or models or [{"name": ""}])[0]["name"],
                "limits": list(search_limits()), "unavailable": ""}

    def search_limits():
        from clip_search import SEARCH_LIMITS
        return SEARCH_LIMITS

    def ranked_photos(db: sqlite3.Connection, text: str, model: str, top: int, clause: str, params: list):
        """Photos matching the filters, best match first: ([(id, score)], model, photos without a vector, limit used)."""
        from clip_search import SearchError
        if search is None:
            raise HTTPException(409, SEARCH_UNAVAILABLE)
        models = search.stored_models(db)
        if not models:
            raise HTTPException(409, "No photos have search vectors yet. Choose option 5 in the menu (or run "
                                     "python sort_photos.py embed --apply), then search again.")
        names = [m["name"] for m in models]
        name = model or next((m["name"] for m in models if m["ready"]), names[0])
        if name not in names:
            raise HTTPException(400, f"There are no search vectors for the model {name}.")
        try:
            ids, scores = search.rank(db, name, text)
        except SearchError as error:
            raise HTTPException(409, str(error))
        allowed = np.fromiter((r[0] for r in db.execute(f"SELECT ph.id FROM photos ph{clause}", params)), dtype=np.int64)
        keep = np.isin(ids, allowed)
        limits = search_limits()
        top = min(limits, key=lambda n: abs(n - top))
        pairs = [(int(i), float(s)) for i, s in zip(ids[keep][:top], scores[keep][:top])]
        return pairs, name, int(len(allowed) - int(keep.sum())), top

    @app.get("/api/photos")
    def list_photos(person: str = "", match: str = "all", uncertain: bool = False, filter: str = "",
                    year: str = "", month: str = "", q: str = "", order: str = "new",
                    d: str = "", model: str = "", top: int = 200,
                    limit: int = 96, offset: int = 0, db: sqlite3.Connection = Depends(get_db)):
        where: list[str] = []
        params: list = []
        try:
            people = [int(p) for p in person.split(",") if p.strip()]
        except ValueError:
            raise HTTPException(400, "Bad person filter.")
        statuses = WITH_UNCERTAIN if uncertain else CONFIDENT
        exists = ("EXISTS (SELECT 1 FROM faces f JOIN face_person fp ON fp.face_id = f.id WHERE f.photo_id = ph.id"
                  " AND fp.person_id {} AND fp.status IN " + statuses + ")")
        if people and match == "any":
            where.append(exists.format(f"IN ({marks(len(people))})"))
            params += people
        else:
            for pid in people:                      # all of them must be in the photo
                where.append(exists.format("= ?"))
                params.append(pid)
        if filter:
            if filter not in FILTERS:
                raise HTTPException(400, "Unknown filter.")
            where.append(FILTERS[filter])
        if year:
            where.append("substr(ph.taken_at, 1, 4) = ?")
            params.append(year)
            if month:
                where.append("substr(ph.taken_at, 6, 2) = ?")
                params.append(month.zfill(2))
        use_fts = photo_meta.has_search(db) if q.strip() else False
        for word in q.split()[:6]:                   # every word is in the file/folder name, or in the notes and tags
            condition, values = photo_meta.word_condition(word, use_fts)
            where.append(condition)
            params += values
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        limit, offset = max(1, min(limit, 300)), max(0, offset)
        described = None
        scores: dict[int, float] = {}
        if d.strip():   # phase 7: best match for the description first, among the photos the other filters allow
            pairs, used, without, top = ranked_photos(db, d, model, top, clause, params)
            total = len(pairs)
            page = pairs[offset:offset + limit]
            scores = dict(page)
            found = {r["id"]: r for r in db.execute(
                f"SELECT id, sorted_path, taken_at FROM photos WHERE id IN ({marks(len(page))})", [i for i, _ in page])} if page else {}
            rows = [found[i] for i, _ in page if i in found]
            described = {"model": used, "without_vectors": without, "top": top}
        else:
            direction = "ASC" if order == "old" else "DESC"
            total = db.execute(f"SELECT COUNT(*) FROM photos ph{clause}", params).fetchone()[0]
            rows = db.execute(
                f"SELECT ph.id, ph.sorted_path, ph.taken_at FROM photos ph{clause}"
                f" ORDER BY ph.taken_at {direction}, ph.id {direction} LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()

        info: dict[int, dict] = {r["id"]: {"people": {}, "faces": 0, "unlabeled": 0} for r in rows}
        ids = list(info)
        for start in range(0, len(ids), SQL_CHUNK):
            part = ids[start:start + SQL_CHUNK]
            for f in db.execute(
                "SELECT f.photo_id, fp.person_id, fp.status, pe.name FROM faces f"
                " LEFT JOIN face_person fp ON fp.face_id = f.id LEFT JOIN persons pe ON pe.id = fp.person_id"
                f" WHERE f.photo_id IN ({marks(len(part))})", part,
            ):
                entry = info[f["photo_id"]]
                if f["status"] == "ignored":
                    continue
                entry["faces"] += 1
                named = f["name"] and f["status"] in ("auto", "confirmed")
                if named:
                    entry["people"][f["person_id"]] = f["name"]
                else:
                    entry["unlabeled"] += 1
        result = {
            "total": total,
            "photos": [
                {"id": r["id"], "name": file_name(r["sorted_path"]), "taken": (r["taken_at"] or "")[:10],
                 "faces": info[r["id"]]["faces"], "unlabeled": info[r["id"]]["unlabeled"],
                 "people": [{"id": pid, "name": name} for pid, name in info[r["id"]]["people"].items()],
                 **({"score": round(scores[r["id"]], 4)} if r["id"] in scores else {})}
                for r in rows
            ],
        }
        if described:
            result["search"] = described
        return result

    # ------------------------------------------------------------ library & chips
    @app.get("/api/library")
    def library(db: sqlite3.Connection = Depends(get_db)):
        years: dict[str, dict] = {}
        for year, month, count in db.execute(
            "SELECT substr(taken_at, 1, 4), substr(taken_at, 6, 2), COUNT(*) FROM photos GROUP BY 1, 2 ORDER BY 1 DESC, 2 DESC"
        ):
            entry = years.setdefault(year, {"year": year, "count": 0, "months": []})
            entry["count"] += count
            entry["months"].append({"month": month, "count": count})
        counts = {name: db.execute(f"SELECT COUNT(*) FROM photos ph WHERE {cond}").fetchone()[0] for name, cond in FILTERS.items()}
        return {"total": sum(y["count"] for y in years.values()), "years": list(years.values()), "counts": counts}

    @app.get("/api/people/chips")
    def people_chips(limit: int = 60, db: sqlite3.Connection = Depends(get_db)):
        rows = db.execute(
            "SELECT p.id, p.name, COUNT(DISTINCT f.photo_id) AS photos,"
            " (SELECT f2.id FROM face_person x JOIN faces f2 ON f2.id = x.face_id WHERE x.person_id = p.id"
            "  AND x.status IN ('auto', 'confirmed') ORDER BY (x.status = 'confirmed') DESC,"
            "  f2.det_score * MIN(f2.face_px, 160) DESC LIMIT 1) AS sample"
            " FROM persons p JOIN face_person fp ON fp.person_id = p.id AND fp.status IN ('auto', 'confirmed')"
            " JOIN faces f ON f.id = fp.face_id WHERE p.name IS NOT NULL AND p.name <> ''"
            " GROUP BY p.id ORDER BY photos DESC, p.name COLLATE NOCASE LIMIT ?", (max(1, min(limit, 300)),),
        ).fetchall()
        unnamed = db.execute(
            "SELECT COUNT(DISTINCT person_id) FROM face_person WHERE status IN ('auto', 'confirmed') AND person_id IN"
            " (SELECT id FROM persons WHERE name IS NULL OR name = '')"
        ).fetchone()[0]
        return {"people": [dict(r) for r in rows], "unnamed_groups": unnamed}
