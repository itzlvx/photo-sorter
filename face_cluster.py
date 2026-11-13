"""Phase 3: group similar faces into people and flag the ones the AI is unsure about.

Every face from phase 2 has an "embedding" (512 numbers). Two faces of the same
person have embeddings that point the same way, so the dot product of two
embeddings (their "similarity", at most 1.0) is high for the same person and
low for different people. This module groups faces by that similarity. It never
names anyone: a group is just "these faces look like the same person" until you
name it in the web UI (phase 4).

How the grouping works
  1. Faces are sorted best-quality first (sharp, frontal, large, confidently
     detected). Only good-quality faces may START a group.
  2. Each good face joins the existing group it matches best if the similarity
     is high enough (--threshold), otherwise it starts a new group.
  3. Groups whose averages are very similar are merged (--merge-threshold).
  4. New groups with only a few faces are dropped: they are usually strangers
     in the background (--min-group-size).
  5. Every face is then compared with the final groups and labelled:
        auto        confident: good quality, similar enough, clear winner
        review      unsure (borderline similarity, two people look alike, or a
                    poor-quality face); the best guess is saved as a suggestion
        unassigned  no good match; not shown in the review queue
     Nothing you decided in the UI is ever changed: named people and faces you
     confirmed are kept, and new faces are matched against them.

Running it again is safe and cheap. By default it only places faces that are new
or still uncertain, and leaves every confident (auto) face where it is.
--recluster lets the AI regroup its own work from scratch (named people and
confirmed faces are still kept).

Needs only numpy and the index database written by phase 2.
"""
from __future__ import annotations

import sqlite3
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np

import sort_photos as sp

log = sp.log

DIM = 512                 # numbers per face embedding
EMBEDDING_BYTES = DIM * 4
BATCH = 2048              # faces compared against all groups at once
EDGE_BLOCK = 1024         # group averages compared per block when looking for merges
MAX_MERGE_ROUNDS = 10
LOW_QUALITY_EXTRA = 0.10  # a poor-quality face must match this much better than --threshold to be trusted

AUTO, REVIEW, UNASSIGNED = 0, 1, 2
STATUS_NAMES = ["auto", "review", "unassigned"]


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------
def has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def open_read_only(db_path) -> sqlite3.Connection:
    return sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True, timeout=30)


@dataclass
class FaceData:
    model: str
    ids: np.ndarray            # face ids, shape (N,)
    emb: np.ndarray            # float32 (N, 512), unit length
    eligible: np.ndarray       # bool (N,): good enough quality to start/define a group
    quality: np.ndarray        # float (N,): higher is better
    skipped: int = 0           # faces whose stored embedding could not be read


def choose_model(conn: sqlite3.Connection, wanted: str | None) -> str | None:
    counts = conn.execute("SELECT model, COUNT(*) FROM faces GROUP BY model ORDER BY 2 DESC").fetchall()
    if not counts:
        return None
    if wanted:
        return wanted
    if len(counts) > 1:
        log.warning(
            "Faces were scanned with more than one model (%s). Embeddings from different models cannot be compared, "
            "so only '%s' is used. Choose another with --model.",
            ", ".join(f"{m}: {c}" for m, c in counts), counts[0][0],
        )
    return counts[0][0]


def load_faces(conn: sqlite3.Connection, model: str, args) -> FaceData:
    total = conn.execute("SELECT COUNT(*) FROM faces WHERE model = ?", (model,)).fetchone()[0]
    ids = np.empty(total, dtype=np.int64)
    emb = np.empty((total, DIM), dtype=np.float32)
    det = np.empty(total, dtype=np.float32)
    px = np.empty(total, dtype=np.float32)
    blur = np.full(total, np.nan, dtype=np.float32)
    yaw = np.full(total, np.nan, dtype=np.float32)
    n = skipped = 0
    cursor = conn.execute(
        "SELECT id, embedding, det_score, face_px, blur, yaw FROM faces WHERE model = ? ORDER BY id", (model,)
    )
    for face_id, blob, det_score, face_px, face_blur, face_yaw in cursor:
        if blob is None or len(blob) != EMBEDDING_BYTES:
            skipped += 1
            continue
        vec = np.frombuffer(blob, dtype=np.float32)
        norm = float(np.linalg.norm(vec))
        if not np.isfinite(norm) or norm == 0.0:
            skipped += 1
            continue
        ids[n] = face_id
        emb[n] = vec / norm
        det[n] = det_score
        px[n] = face_px
        if face_blur is not None:
            blur[n] = face_blur
        if face_yaw is not None:
            yaw[n] = face_yaw
        n += 1
    ids, emb, det, px, blur, yaw = ids[:n], emb[:n], det[:n], px[:n], blur[:n], yaw[:n]

    eligible = (det >= args.min_det_score) & (px >= args.min_face_px)
    eligible &= np.isnan(blur) | (blur >= args.min_blur)      # unknown sharpness/yaw: give the benefit of the doubt
    eligible &= np.isnan(yaw) | (np.abs(yaw) <= args.max_yaw)
    yaw_penalty = 1.0 - 0.5 * np.clip(np.nan_to_num(np.abs(yaw), nan=0.0), 0, 90) / 90.0
    quality = det * np.clip(px / 160.0, 0.0, 1.0) * yaw_penalty
    return FaceData(model=model, ids=ids, emb=emb, eligible=eligible, quality=quality, skipped=skipped)


@dataclass
class State:
    persons: dict[int, str | None] = field(default_factory=dict)       # person id -> name
    rows: dict[int, tuple[int | None, str]] = field(default_factory=dict)  # face id -> (person id, status)
    rejected: dict[int, set[int]] = field(default_factory=dict)        # face id -> person ids it is NOT


def load_state(conn: sqlite3.Connection) -> State:
    state = State()
    if has_table(conn, "persons"):
        state.persons = {pid: name for pid, name in conn.execute("SELECT id, name FROM persons")}
    if has_table(conn, "face_person"):
        state.rows = {fid: (pid, status) for fid, pid, status in conn.execute("SELECT face_id, person_id, status FROM face_person")}
    if has_table(conn, "face_rejections"):
        for fid, pid in conn.execute("SELECT face_id, person_id FROM face_rejections"):
            state.rejected.setdefault(fid, set()).add(pid)
    return state


# --------------------------------------------------------------------------
# Groups (running averages of embeddings)
# --------------------------------------------------------------------------
class Groups:
    """A growing set of groups. 'seed' groups are people that already exist in the
    database; 'locked' seeds (named, or holding a face you confirmed) keep a frozen
    average so the AI can never pull them off course."""

    def __init__(self, capacity: int = 1024):
        self.n = 0
        self.sums = np.zeros((capacity, DIM), dtype=np.float32)
        self.cent = np.zeros((capacity, DIM), dtype=np.float32)
        self.counts = np.zeros(capacity, dtype=np.int64)
        self.seed = np.zeros(capacity, dtype=bool)
        self.locked = np.zeros(capacity, dtype=bool)
        self.alive = np.zeros(capacity, dtype=bool)
        self.pid = np.full(capacity, -1, dtype=np.int64)

    def _grow(self) -> None:
        cap = len(self.counts) * 2
        for name in ("sums", "cent"):
            old = getattr(self, name)
            new = np.zeros((cap, DIM), dtype=old.dtype)
            new[: len(old)] = old
            setattr(self, name, new)
        for name, fill in (("counts", 0), ("seed", False), ("locked", False), ("alive", False), ("pid", -1)):
            old = getattr(self, name)
            new = np.full(cap, fill, dtype=old.dtype)
            new[: len(old)] = old
            setattr(self, name, new)

    def add(self, vec_sum: np.ndarray, count: int, pid: int = -1, seed: bool = False, locked: bool = False) -> int:
        if self.n == len(self.counts):
            self._grow()
        k = self.n
        self.n += 1
        self.sums[k] = vec_sum
        self.counts[k] = count
        self.seed[k] = seed
        self.locked[k] = locked
        self.alive[k] = True
        self.pid[k] = pid
        self._normalise(k)
        return k

    def _normalise(self, k: int) -> None:
        norm = float(np.linalg.norm(self.sums[k]))
        self.cent[k] = self.sums[k] / norm if norm > 0 else 0.0

    def join(self, k: int, vec: np.ndarray) -> None:
        self.sums[k] += vec
        self.counts[k] += 1
        if not self.locked[k]:
            self._normalise(k)

    def alive_indices(self) -> np.ndarray:
        return np.nonzero(self.alive[: self.n])[0]


def merge_groups(groups: Groups, threshold: float) -> int:
    """Merge groups whose averages are very similar. Two existing people are never
    merged (that is your call in the UI). Returns the number of merges."""
    total = 0
    for _ in range(MAX_MERGE_ROUNDS):
        alive = groups.alive_indices()
        if len(alive) < 2:
            break
        centres = groups.cent[alive]
        edges: list[tuple[float, int, int]] = []
        for start in range(0, len(alive), EDGE_BLOCK):
            block = centres[start:start + EDGE_BLOCK] @ centres.T
            rows, cols = np.nonzero(block >= threshold)
            for r, c in zip(rows.tolist(), cols.tolist()):
                if c > start + r:
                    edges.append((float(block[r, c]), alive[start + r], alive[c]))
        if not edges:
            break
        edges.sort(key=lambda e: -e[0])
        used: set[int] = set()
        merged = 0
        for _, a, b in edges:
            if a in used or b in used or (groups.seed[a] and groups.seed[b]):
                continue
            if groups.seed[a]:
                target, source = a, b
            elif groups.seed[b]:
                target, source = b, a
            else:
                target, source = (a, b) if groups.counts[a] >= groups.counts[b] else (b, a)
            groups.sums[target] += groups.sums[source]
            groups.counts[target] += groups.counts[source]
            groups.alive[source] = False
            if not groups.locked[target]:
                groups._normalise(target)
            used.update((a, b))
            merged += 1
        if not merged:
            break
        total += merged
    return total


# --------------------------------------------------------------------------
# Planning
# --------------------------------------------------------------------------
@dataclass
class Plan:
    groups: Groups
    pending: np.ndarray                 # indices (into FaceData) of faces placed this run
    group_of: np.ndarray                # per pending face: group index or -1
    similarity: np.ndarray              # per pending face: similarity to its group
    status: np.ndarray                  # per pending face: AUTO / REVIEW / UNASSIGNED
    reason: list[str | None]            # per pending face
    kept_count: int                     # faces left exactly as they were
    kept_by_status: Counter
    seed_count: int
    new_groups: int                     # groups that will be created
    merged: int
    dissolved: int                      # new groups dropped as too small
    delete_person_ids: list[int]        # unnamed, empty people that will be removed
    seed_sizes: dict[int, int] = field(default_factory=dict)  # faces kept per existing-person group


def classify(groups: Groups, pending: np.ndarray, data: FaceData, rejected_idx: dict[int, set[int]],
             pid_to_k: dict[int, int], args):
    """Compare every pending face with every live group and decide its label."""
    count = len(pending)
    best_k = np.full(count, -1, dtype=np.int64)
    best_s = np.full(count, -1.0, dtype=np.float32)
    second_s = np.full(count, -1.0, dtype=np.float32)
    alive = groups.alive_indices()
    if len(alive) and count:
        centres = groups.cent[alive]
        col_of = {int(k): c for c, k in enumerate(alive)}
        for start in range(0, count, BATCH):
            sel = pending[start:start + BATCH]
            sims = data.emb[sel] @ centres.T
            for r, i in enumerate(sel.tolist()):
                for pid in rejected_idx.get(i, ()):  # "this is not person X" answers from the UI
                    col = col_of.get(pid_to_k.get(pid, -1))
                    if col is not None:
                        sims[r, col] = -1.0
            rows = np.arange(len(sel))
            top = sims.argmax(axis=1)
            best_s[start:start + len(sel)] = sims[rows, top]
            best_k[start:start + len(sel)] = alive[top]
            if sims.shape[1] > 1:
                sims[rows, top] = -2.0
                second_s[start:start + len(sel)] = sims.max(axis=1)

    eligible = data.eligible[pending]
    gap = best_s - second_s
    need = np.where(eligible, args.threshold, args.threshold + LOW_QUALITY_EXTRA)
    is_auto = (best_s >= need) & (gap >= args.margin)
    is_review = (best_s >= args.review_threshold) & ~is_auto
    status = np.where(is_auto, AUTO, np.where(is_review, REVIEW, UNASSIGNED))

    reason: list[str | None] = []
    for s, ok, b, n, g in zip(status.tolist(), eligible.tolist(), best_s.tolist(), need.tolist(), gap.tolist()):
        if s == AUTO:
            reason.append(None)
        elif s == REVIEW:
            reason.append("ambiguous" if b >= n else ("low_quality" if not ok else "borderline"))
        else:
            reason.append("low_quality" if not ok else "no_match")
    person_k = np.where(status == UNASSIGNED, -1, best_k)
    return person_k, best_s, status, reason


def make_plan(data: FaceData, state: State, args) -> Plan:
    n = len(data.ids)
    index_of = {int(fid): i for i, fid in enumerate(data.ids.tolist())}

    # --- what the human already decided, and what the AI did last time ---------
    named = {pid for pid, name in state.persons.items() if name and name.strip()}
    confirmed_people = {pid for pid, status in state.rows.values() if status == "confirmed" and pid is not None}
    locked_ids = named | confirmed_people

    kept = np.zeros(n, dtype=bool)
    kept_person = np.full(n, -1, dtype=np.int64)
    kept_by_status: Counter = Counter()
    for fid, (pid, status) in state.rows.items():
        i = index_of.get(fid)
        if i is None or pid is None or pid not in state.persons:
            continue
        keep = status == "confirmed" or (status == "auto" and (not args.recluster or pid in locked_ids))
        if keep:
            kept[i] = True
            kept_person[i] = pid
            kept_by_status[status] += 1

    groups = Groups()
    pid_to_k: dict[int, int] = {}
    kept_idx = np.nonzero(kept)[0]
    if len(kept_idx):
        order = np.argsort(kept_person[kept_idx], kind="stable")
        sorted_idx = kept_idx[order]
        sorted_pid = kept_person[sorted_idx]
        starts = np.r_[0, np.nonzero(np.diff(sorted_pid))[0] + 1]
        sums = np.add.reduceat(data.emb[sorted_idx], starts, axis=0)
        sizes = np.diff(np.r_[starts, len(sorted_idx)])
        for row, pid in enumerate(sorted_pid[starts].tolist()):
            pid_to_k[pid] = groups.add(sums[row], int(sizes[row]), pid=pid, seed=True, locked=pid in locked_ids)
    seed_count = groups.n
    seed_sizes = {k: int(groups.counts[k]) for k in range(seed_count)}  # before any new face joins

    pending = np.nonzero(~kept)[0]
    rejected_idx = {index_of[f]: p for f, p in state.rejected.items() if f in index_of and not kept[index_of[f]]}

    # --- 1. form groups from the good-quality pending faces, best first -------
    candidates = pending[data.eligible[pending]]
    candidates = candidates[np.argsort(-data.quality[candidates], kind="stable")]
    for i in candidates.tolist():
        vec = data.emb[i]
        k = -1
        if groups.n:
            sims = groups.cent[: groups.n] @ vec
            for pid in rejected_idx.get(i, ()):
                if pid in pid_to_k:
                    sims[pid_to_k[pid]] = -1.0
            best = int(sims.argmax())
            if sims[best] >= args.threshold:
                k = best
        if k >= 0:
            groups.join(k, vec)
        else:
            groups.add(vec, 1)
    log.debug("Leader pass: %d candidate faces -> %d groups (%d existing people)", len(candidates), groups.n, seed_count)

    # --- 2. merge groups that are clearly the same person ---------------------
    merged = merge_groups(groups, args.merge_threshold)
    log.debug("Merged %d groups", merged)

    # --- 3. drop small new groups (strangers), then label every pending face --
    dissolved = 0

    def drop_small_new_groups(minimum: int, count_of: np.ndarray) -> int:
        dropped = 0
        for k in groups.alive_indices().tolist():
            if not groups.seed[k] and count_of[k] < minimum:
                groups.alive[k] = False
                dropped += 1
        return dropped

    dissolved += drop_small_new_groups(args.min_group_size, groups.counts)
    attempts = 0
    while True:
        person_k, similarity, status, reason = classify(groups, pending, data, rejected_idx, pid_to_k, args)
        auto_counts = np.bincount(person_k[status == AUTO], minlength=groups.n)
        weak = [
            k for k in groups.alive_indices().tolist()
            if not groups.seed[k] and auto_counts[k] < args.min_group_size
        ]
        if not weak:
            break
        for k in weak:
            groups.alive[k] = False
        dissolved += len(weak)
        attempts += 1
        if attempts > 5:  # settle: classify once more without the weak groups and accept the result
            person_k, similarity, status, reason = classify(groups, pending, data, rejected_idx, pid_to_k, args)
            break

    # --- people that will disappear: unnamed, no faces left in them -----------
    keep_ids = set(locked_ids) | set(pid_to_k)
    delete_ids = sorted(pid for pid in state.persons if pid not in keep_ids)
    new_groups = int(sum(1 for k in groups.alive_indices().tolist() if not groups.seed[k]))
    return Plan(
        groups=groups, pending=pending, group_of=person_k, similarity=similarity, status=status, reason=reason,
        kept_count=int(kept.sum()), kept_by_status=kept_by_status, seed_count=seed_count,
        new_groups=new_groups, merged=merged, dissolved=dissolved, delete_person_ids=delete_ids,
        seed_sizes=seed_sizes,
    )


# --------------------------------------------------------------------------
# Saving
# --------------------------------------------------------------------------
def save_plan(conn: sqlite3.Connection, plan: Plan, data: FaceData) -> dict[int, int]:
    """Write the plan in one transaction: either everything is saved or nothing is."""
    now = datetime.now().isoformat(timespec="seconds")
    groups = plan.groups
    used_new = Counter(plan.group_of[plan.group_of >= 0].tolist())
    k_to_pid: dict[int, int] = {int(k): int(groups.pid[k]) for k in groups.alive_indices().tolist() if groups.seed[k]}
    with conn:
        new_ids = [k for k in groups.alive_indices().tolist() if not groups.seed[k]]
        new_ids.sort(key=lambda k: -used_new.get(k, 0))  # biggest group gets the lowest id
        for k in new_ids:
            cursor = conn.execute("INSERT INTO persons (name, created_at) VALUES (NULL, ?)", (now,))
            k_to_pid[k] = int(cursor.lastrowid)
        rows = []
        for i, k, sim, status, reason in zip(
            plan.pending.tolist(), plan.group_of.tolist(), plan.similarity.tolist(), plan.status.tolist(), plan.reason
        ):
            person = k_to_pid.get(k) if k >= 0 else None
            rows.append((
                int(data.ids[i]), person, round(float(sim), 4) if person is not None else None,
                STATUS_NAMES[status], reason, now,
            ))
        conn.executemany(
            "INSERT OR REPLACE INTO face_person (face_id, person_id, similarity, status, reason, assigned_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )
        if plan.delete_person_ids:
            conn.executemany("DELETE FROM persons WHERE id = ?", [(pid,) for pid in plan.delete_person_ids])
    return k_to_pid


# --------------------------------------------------------------------------
# Command
# --------------------------------------------------------------------------
def cluster_faces(args) -> int:
    started = time.monotonic()
    db_path = (args.db or sp.default_db_path()).resolve()
    log.info("=== Grouping faces into people started (%s) ===", "apply" if args.apply else "dry run")
    log.info(
        "Join a person at similarity >= %.2f | suggest from >= %.2f | merge groups >= %.2f | runner-up margin %.2f | smallest new group %d",
        args.threshold, args.review_threshold, args.merge_threshold, args.margin, args.min_group_size,
    )
    if not (0.0 < args.review_threshold <= args.threshold < 1.0):
        log.error("Thresholds must satisfy 0 < --review-threshold <= --threshold < 1.")
        return 2
    if args.merge_threshold < args.threshold:
        log.warning("--merge-threshold is lower than --threshold; groups may merge people who should stay separate.")
    if args.min_group_size < 1:
        log.error("--min-group-size must be at least 1.")
        return 2
    sp.log_index_location(db_path, args.apply)
    if not db_path.exists():
        log.error("There is no index yet. Run 'sort ... --apply', 'refresh ... --apply' and then 'faces --apply' first.")
        return 2
    try:
        conn = sp.open_index(db_path) if args.apply else open_read_only(db_path)
    except (OSError, sqlite3.Error) as error:
        log.error("Cannot open the index %s: %s", db_path, error)
        return 2

    try:
        if not has_table(conn, "faces"):
            log.error("The index has no faces yet. Run 'faces --apply' first.")
            return 2
        model = choose_model(conn, args.model)
        if model is None:
            log.error("No faces are stored yet. Run 'faces --apply' first.")
            return 2
        data = load_faces(conn, model, args)
        if not len(data.ids):
            log.error("No usable faces found for model '%s'.", model)
            return 2
        state = load_state(conn)
        log.info("Faces loaded: %d (model %s), %d good enough to define a person", len(data.ids), model, int(data.eligible.sum()))
        if data.skipped:
            log.warning("%d faces were skipped because their stored embedding is unreadable (rescan them with 'faces --rescan').", data.skipped)
        named = sum(1 for name in state.persons.values() if name and name.strip())
        log.info(
            "Already in the index: %d people (%d named), %d face assignments%s",
            len(state.persons), named, len(state.rows), "; the AI's own grouping will be redone" if args.recluster else "",
        )

        plan = make_plan(data, state, args)
        log_report(plan, data, state, args)

        if not args.apply:
            log.info("Dry run: nothing was saved. Add --apply to save this grouping.")
        else:
            try:
                save_plan(conn, plan, data)
            except sqlite3.Error as error:
                log.error("Could not save the grouping (nothing was changed): %s", error)
                return 1
            log.info("Saved. Open the web UI (phase 4) to name the people and review the uncertain faces.")
        log.info("Finished in %.1f s.", time.monotonic() - started)
        return 0
    finally:
        conn.close()


def log_report(plan: Plan, data: FaceData, state: State, args) -> None:
    groups = plan.groups
    status = plan.status
    count = len(plan.pending)
    auto = int((status == AUTO).sum())
    review = int((status == REVIEW).sum())
    unassigned = int((status == UNASSIGNED).sum())
    reasons = Counter(r for r in plan.reason if r)
    reason_of = lambda s, r: sum(1 for st, rr in zip(status.tolist(), plan.reason) if st == s and rr == r)  # noqa: E731

    log.info("")
    log.info("%-26s%d", "Faces left as they were:", plan.kept_count)
    if plan.kept_count:
        log.info("%-26s%d confirmed by you, %d confident from earlier runs", "", plan.kept_by_status.get("confirmed", 0), plan.kept_by_status.get("auto", 0))
    log.info("%-26s%d", "Faces placed now:", count)
    log.info("%-26s%d confident (auto)", "", auto)
    log.info(
        "%-26s%d to review (%d borderline, %d two people look alike, %d poor quality)",
        "", review, reason_of(REVIEW, "borderline"), reason_of(REVIEW, "ambiguous"), reason_of(REVIEW, "low_quality"),
    )
    log.info(
        "%-26s%d with no good match (%d no match, %d poor quality)",
        "", unassigned, reason_of(UNASSIGNED, "no_match"), reason_of(UNASSIGNED, "low_quality"),
    )
    log.info("")
    people_after = int(sum(1 for k in groups.alive_indices().tolist() if groups.seed[k])) + plan.new_groups
    log.info("%-26s%d new, %d existing kept", "People (groups):", plan.new_groups, plan.seed_count)
    if plan.merged:
        log.info("%-26s%d groups merged into others", "", plan.merged)
    if plan.dissolved:
        log.info("%-26s%d small groups dropped (their faces were re-checked against the rest)", "", plan.dissolved)
    if plan.delete_person_ids:
        log.info("%-26s%d unnamed empty groups removed", "", len(plan.delete_person_ids))
    log.info("%-26s%d", "People after this run:", people_after)

    sims = plan.similarity[status == AUTO]
    if len(sims):
        low, p5, med = np.percentile(sims, [0, 5, 50])
        log.info("%-26smin %.2f, 5th percentile %.2f, median %.2f", "Similarity of auto faces:", low, p5, med)

    # biggest groups: faces left as they were (seed groups) + confident faces placed now
    total: Counter = Counter()
    for k in groups.alive_indices().tolist():
        if groups.seed[k]:
            total[k] = int(plan.seed_sizes.get(k, 0))
    for k, s in zip(plan.group_of.tolist(), status.tolist()):
        if k >= 0 and s == AUTO:
            total[k] += 1
    top = [(k, c) for k, c in total.most_common(10) if c]
    if top:
        log.info("")
        log.info("Largest groups:")
        for k, c in top:
            if groups.seed[k]:
                pid = int(groups.pid[k])
                label = state.persons.get(pid) or "unnamed"
                log.info("  #%-6d %-22s %d faces", pid, label, c)
            else:
                log.info("  %-7s %-22s %d faces", "new", "unnamed", c)
    if review:
        log.info("")
        log.info("%d faces are waiting for your review in the web UI.", review)
    if reasons.get("borderline", 0) + reasons.get("ambiguous", 0) > max(50, 0.3 * count):
        log.info("Tip: many faces are uncertain. Check a few groups in the UI, then adjust --threshold / --margin and run again with --recluster.")
