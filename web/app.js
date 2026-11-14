"use strict";
/* Photo sorter interface. Plain JavaScript, no build step, no network access except this program. */

// ---------------------------------------------------------------- helpers
const main = document.getElementById("main");
const rail = document.getElementById("rail");
const selbar = document.getElementById("selbar");
const toastEl = document.getElementById("toast");

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === false || value == null) continue;
    if (key === "class") el.className = value;
    else if (key === "text") el.textContent = value;
    else if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
    else el.setAttribute(key, value === true ? "" : value);
  }
  for (const kid of kids.flat()) {
    if (kid == null || kid === false) continue;
    el.append(kid.nodeType ? kid : document.createTextNode(kid));
  }
  return el;
}

class ApiError extends Error {
  constructor(message, status, data) { super(message); this.status = status; this.data = data; }
}

async function api(path, options = {}) {
  const init = { method: "GET", headers: {} };
  if (options.body !== undefined) {
    init.method = "POST";
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(options.body);
  }
  if (init.method !== "GET") init.headers["X-Photo-Sorter"] = "1";
  let response;
  try { response = await fetch(path, init); }
  catch { throw new ApiError("Can't reach the photo sorter. Check that the window running it is still open.", 0); }
  let data = null;
  try { data = await response.json(); } catch { /* not JSON */ }
  if (!response.ok) {
    const detail = data && typeof data.detail === "string" ? data.detail : `Something went wrong (error ${response.status}).`;
    throw new ApiError(detail, response.status, data);
  }
  return data;
}

const plural = (n, word) => `${n.toLocaleString()} ${word}${n === 1 ? "" : "s"}`;
const thumb = (id) => `/api/faces/${id}/thumb`;
const personLabel = (p) => p.name || "Unnamed person";
const REASONS = {
  borderline: "Close, not sure",
  ambiguous: "Looks like two people",
  low_quality: "Hard to see",
  rejected: "Not that person",
  restored: "Restored",
  no_match: "No match",
};

let toastTimer;
function toast(message, kind = "") {
  toastEl.textContent = message;
  toastEl.className = "toast" + (kind ? " " + kind : "");
  toastEl.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { toastEl.hidden = true; }, kind === "error" ? 7000 : 3500);
}

// ---------------------------------------------------------------- dialogs
function openDialog(content, { wide = false, onClose } = {}) {
  const dialog = h("dialog", { class: wide ? "wide" : "" }, content);
  document.body.append(dialog);
  dialog.addEventListener("close", () => { dialog.remove(); if (onClose) onClose(); });
  dialog.addEventListener("click", (event) => { if (event.target === dialog) dialog.close(); });
  dialog.showModal();
  return dialog;
}

function ask({ title, text, confirmLabel, cancelLabel = "Cancel", danger = false }) {
  return new Promise((resolve) => {
    let answer = false;
    const ok = h("button", { class: "btn " + (danger ? "danger" : "primary"), type: "button", text: confirmLabel });
    const cancel = h("button", { class: "btn", type: "button", text: cancelLabel });
    const dialog = openDialog(
      h("div", { class: "dlg" }, h("h2", { text: title }), text ? h("p", { text }) : null, h("div", { class: "row" }, cancel, ok)),
      { onClose: () => resolve(answer) },
    );
    ok.addEventListener("click", () => { answer = true; dialog.close(); });
    cancel.addEventListener("click", () => dialog.close());
    ok.focus();
  });
}

function personRow(person, onClick, right) {
  const sample = person.samples && person.samples[0];
  return h("button", { class: "pick", type: "button", onclick: onClick },
    sample ? h("img", { src: thumb(sample), alt: "" }) : h("span", { class: "blank" }),
    h("span", {}, h("span", { class: "t" + (person.name ? "" : " unnamed"), text: personLabel(person) }),
      h("br"), h("span", { class: "count", text: plural(person.faces, "face") })),
    h("span", { class: "count", text: right || "" }),
  );
}

/** Choose a person (or start a new one). onPick receives {person} or {newName}. */
function openPicker({ title, text, suggestions = [], exclude = null, allowNew = false, onPick }) {
  const list = h("div", { class: "pick-list" });
  const search = h("input", { class: "field", type: "search", placeholder: "Search people by name", "aria-label": "Search people" });
  let dialog;
  const choose = (value) => { dialog.close(); onPick(value); };

  async function fill(query) {
    const rows = [];
    const shown = suggestions.filter((s) => s.id !== exclude);
    if (!query && shown.length) {
      rows.push(h("h2", { class: "sub", text: "Looks most like" }));
      for (const s of shown) rows.push(personRow(s, () => choose({ person: s }), `match ${s.similarity.toFixed(2)}`));
    }
    const data = await api(`/api/people?${new URLSearchParams({ named: query ? "all" : "named", q: query, sort: "name", limit: 80 })}`);
    const suggested = new Set(query ? [] : shown.map((s) => s.id));
    const found = data.people.filter((p) => p.id !== exclude && !suggested.has(p.id));
    if (!query && shown.length && found.length) rows.push(h("h2", { class: "sub", text: "Other named people" }));
    for (const p of found) rows.push(personRow(p, () => choose({ person: p })));
    if (!found.length && !(rows.length && !query)) rows.push(h("p", { class: "count", text: query ? "No one with that name." : "No one is named yet. Create a person below." }));
    list.replaceChildren(...rows);
  }

  const parts = [h("h2", { text: title }), text ? h("p", { text }) : null, search, list];
  if (allowNew) {
    const name = h("input", { class: "field", type: "text", placeholder: "Name for a new person (optional)", maxlength: "80", "aria-label": "Name for a new person" });
    const create = h("button", { class: "btn primary", type: "button", text: "Create person", onclick: () => choose({ newName: name.value.trim() }) });
    name.addEventListener("keydown", (e) => { if (e.key === "Enter") create.click(); });
    parts.push(h("div", { class: "new-row" }, name, create));
  }
  parts.push(h("div", { class: "row" }, h("button", { class: "btn", type: "button", text: "Cancel", onclick: () => dialog.close() })));
  dialog = openDialog(h("div", { class: "dlg" }, ...parts));
  let timer;
  search.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(() => fill(search.value.trim()), 200); });
  fill("").catch((e) => toast(e.message, "error"));
  search.focus();
}

async function openContext(faceId) {
  const body = h("div", { class: "loading", text: "Opening the photo…" });
  const meta = h("p", {});
  const dialog = openDialog(h("div", { class: "dlg" }, body, meta,
    h("div", { class: "row" }, h("button", { class: "btn", type: "button", text: "Close", onclick: () => dialog.close() }))), { wide: true });
  try {
    const [info, response] = await Promise.all([api(`/api/faces/${faceId}`), fetch(`/api/faces/${faceId}/photo`)]);
    meta.textContent = `${info.taken ? "Taken " + info.taken + ". " : ""}${info.path}`;
    if (!response.ok) {
      let detail = "The photo could not be opened.";
      try { detail = (await response.json()).detail || detail; } catch { /* keep default */ }
      body.textContent = detail;
      return;
    }
    const url = URL.createObjectURL(await response.blob());
    body.replaceWith(h("img", { class: "context-img", src: url, alt: "The whole photo, with this face outlined in yellow" }));
  } catch (e) { body.textContent = e.message; }
}

// ---------------------------------------------------------------- state
const state = {
  summary: null,
  faces: [],            // faces currently shown in the grid
  tiles: new Map(),     // face id -> tile element
  sel: new Set(),
  last: -1,             // index of the last clicked tile (for shift-click ranges)
  actions: null,        // () => [buttons] for the selection bar
  reload: null,         // reloads the current view
  filter: { named: "all", q: "", sort: "size" },
};
let renderToken = 0;

async function refreshSummary() {
  state.summary = await api("/api/summary");
  renderRail();
}

function renderRail() {
  const s = state.summary || {};
  const here = location.hash || "#/people";
  const link = (href, label, count, alert) => h("a", { class: "nav-link", href, "aria-current": here.startsWith(href) ? "page" : false },
    h("span", { text: label }), count != null ? h("span", { class: "count" + (alert && count > 0 ? " is-alert" : ""), text: count.toLocaleString() }) : null);
  const foot = h("div", { class: "rail-foot" },
    s.ungrouped > 0 ? h("p", { class: "rail-note", text: `${plural(s.ungrouped, "new face")} not grouped yet.` }) : null,
    h("button", { class: "btn" + (s.ungrouped > 0 ? " primary" : ""), type: "button", text: "Place new faces", onclick: regroup }),
  );
  rail.replaceChildren(
    h("div", { class: "brand", text: "Photo sorter" }),
    link("#/photos", "Photos", s.photos),
    link("#/people", "People", s.people), link("#/review", "Review", s.review, true),
    link("#/unassigned", "Unassigned", s.unassigned), link("#/ignored", "Ignored", s.ignored),
    foot,
  );
}

async function regroup() {
  const output = h("pre", { text: "Grouping faces…" });
  const close = h("button", { class: "btn primary", type: "button", text: "Done", disabled: true });
  const dialog = openDialog(h("div", { class: "dlg" }, h("h2", { text: "Placing new faces" }),
    h("p", { text: "New faces are matched to the people you already named. Your confirmed choices never change." }), output, h("div", { class: "row" }, close)),
    { onClose: async () => { await refreshSummary().catch(() => {}); if (state.reload) state.reload(); } });
  close.addEventListener("click", () => dialog.close());
  try {
    const result = await api("/api/regroup", { body: {} });
    output.textContent = result.lines.join("\n") || "Done.";
  } catch (e) { output.textContent = e.message; }
  close.disabled = false;
  close.focus();
}

// ---------------------------------------------------------------- selection
function clearSelection() { state.sel.clear(); state.last = -1; syncTiles(); renderSelbar(); }

function syncTiles() {
  for (const [id, el] of state.tiles) {
    const on = state.sel.has(id);
    el.classList.toggle("is-selected", on);
    el.querySelector(".tile-pick").setAttribute("aria-pressed", String(on));
  }
}

function toggleFace(id, event) {
  const index = state.faces.findIndex((f) => f.id === id);
  if (event.shiftKey && state.last >= 0 && index >= 0) {
    const [from, to] = [Math.min(state.last, index), Math.max(state.last, index)];
    for (let i = from; i <= to; i++) state.sel.add(state.faces[i].id);
  } else if (state.sel.has(id)) state.sel.delete(id);
  else state.sel.add(id);
  state.last = index;
  syncTiles();
  renderSelbar();
}

function renderSelbar() {
  if (!state.sel.size || !state.actions) { selbar.hidden = true; return; }
  selbar.hidden = false;
  selbar.replaceChildren(
    h("span", { class: "n", text: `${state.sel.size.toLocaleString()} selected` }),
    h("button", { class: "btn quiet", type: "button", text: "Select all shown", onclick: () => { state.faces.forEach((f) => state.sel.add(f.id)); syncTiles(); renderSelbar(); } }),
    h("button", { class: "btn quiet", type: "button", text: "Clear", onclick: clearSelection }),
    h("span", { class: "spacer" }),
    ...state.actions(),
  );
}

const selectedIds = () => [...state.sel];

async function changeFaces(path, ids, message, extra = {}) {
  try {
    const result = await api(path, { body: { face_ids: ids, ...extra } });
    toast(typeof message === "function" ? message(result) : message);
    clearSelection();
    await refreshSummary();
    if (state.reload) await state.reload();
    return result;
  } catch (e) { toast(e.message, "error"); return null; }
}

function moveTo(current = null) {
  const ids = selectedIds();
  api("/api/faces/suggest", { body: { face_ids: ids } }).catch(() => ({ people: [] })).then(({ people }) => {
    openPicker({
      title: `Move ${plural(ids.length, "face")} to…`,
      text: "These faces will count as confirmed by you.",
      suggestions: people, allowNew: true, exclude: current,
      onPick: ({ person, newName }) => changeFaces("/api/faces/assign", ids,
        person ? `Moved ${plural(ids.length, "face")} to ${personLabel(person)}` : `Started ${newName || "a new person"} with ${plural(ids.length, "face")}`,
        person ? { person_id: person.id } : { new_person: true, name: newName }),
    });
  });
}

const ignoreAction = () => h("button", { class: "btn", type: "button", text: "Ignore", title: "Not a real face, or someone you don't need to track",
  onclick: () => changeFaces("/api/faces/ignore", selectedIds(), (r) => `Ignored ${plural(r.changed, "face")}`) });

// ---------------------------------------------------------------- face grid
function faceTile(face, mode) {
  const caption = [];
  if (face.status === "confirmed") caption.push(h("span", { class: "ok", text: "✓ Confirmed" }));
  else if (face.status === "review" || face.status === "unassigned") {
    if (face.reason) caption.push(h("span", { class: face.status === "review" ? "why" : "", text: REASONS[face.reason] || face.reason }));
  } else if (face.similarity != null) {
    caption.push(h("span", { class: "fit", title: `Similarity ${face.similarity.toFixed(2)} (1.00 is identical)` }, h("i", { style: `width:${Math.max(0, Math.min(1, face.similarity)) * 100}%` })));
    caption.push(face.taken);
  }
  if (!caption.length) caption.push(face.taken);
  const tile = h("div", { class: "tile" },
    h("button", { class: "tile-pick", type: "button", "aria-pressed": "false", "aria-label": `Face from ${face.taken || "an undated photo"}`, onclick: (e) => toggleFace(face.id, e) },
      h("img", { src: thumb(face.id), alt: "", loading: "lazy" })),
    h("span", { class: "tick", "aria-hidden": "true", text: "✓" }),
    h("button", { class: "zoom", type: "button", title: "Show the whole photo", "aria-label": "Show the whole photo", onclick: () => openContext(face.id), text: "⤢" }),
    h("div", { class: "cap" }, ...caption),
  );
  if (state.sel.has(face.id)) { tile.classList.add("is-selected"); tile.querySelector(".tile-pick").setAttribute("aria-pressed", "true"); }
  return tile;
}

/** A paged grid of faces. Returns {el, load}. */
function faceGrid(query, { empty, onTotal } = {}) {
  const grid = h("div", { class: "faces" });
  const more = h("button", { class: "btn", type: "button", text: "Show more", hidden: true });
  const emptyBox = h("div", { class: "empty", hidden: true }, empty || h("p", { text: "Nothing here." }));
  const el = h("div", {}, grid, h("p", {}, more), emptyBox);
  let offset = 0;
  async function load(reset) {
    if (reset) { offset = 0; state.faces = []; state.tiles = new Map(); grid.replaceChildren(); }
    const data = await api(`/api/faces?${new URLSearchParams({ ...query, limit: 120, offset })}`);
    for (const face of data.faces) {
      state.faces.push(face);
      const tile = faceTile(face);
      state.tiles.set(face.id, tile);
      grid.append(tile);
    }
    offset += data.faces.length;
    more.hidden = offset >= data.total;
    emptyBox.hidden = data.total > 0;
    if (onTotal) onTotal(data.total);
    return data.total;
  }
  more.addEventListener("click", () => load(false).catch((e) => toast(e.message, "error")));
  return { el, load };
}

// ---------------------------------------------------------------- name field
function nameField(person, { big = false, onMerged } = {}) {
  const input = h("input", { class: "name" + (big ? " name-big" : ""), type: "text", value: person.name, placeholder: "Add a name", "aria-label": "Name", maxlength: "80", spellcheck: "false" });
  let saved = person.name;
  async function commit() {
    const value = input.value.replace(/\s+/g, " ").trim();
    if (value === saved) { input.value = saved; return; }
    try {
      const result = await api(`/api/people/${person.id}/name`, { body: { name: value } });
      saved = result.name; input.value = saved; person.name = saved;
      toast(saved ? `Saved the name ${saved}` : "Removed the name");
      await refreshSummary();
    } catch (e) {
      if (e.status === 409 && e.data) {
        input.value = saved;
        const yes = await ask({ title: `${e.data.conflict_name} already exists`, text: "Merge these two groups into one person? Their faces will be combined.", confirmLabel: "Merge them", cancelLabel: "Keep separate" });
        if (yes) {
          try {
            const merged = await api(`/api/people/${person.id}/name`, { body: { name: value, merge_if_exists: true } });
            toast(`Merged into ${merged.name}`);
            await refreshSummary();
            if (onMerged) onMerged(merged.merged_into);
          } catch (err) { toast(err.message, "error"); }
        }
      } else { toast(e.message, "error"); input.value = saved; }
    }
  }
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") input.blur(); if (e.key === "Escape") { input.value = saved; input.blur(); } });
  input.addEventListener("blur", commit);
  return input;
}

// ---------------------------------------------------------------- views
function head(title, text, ...extra) {
  return h("div", { class: "page-head" }, h("div", {}, h("h1", { text: title }), text ? h("p", { text }) : null), h("span", { class: "spacer" }), ...extra);
}

function banners() {
  const s = state.summary || {};
  const out = [];
  if (s.review > 0 && !location.hash.startsWith("#/review")) {
    out.push(h("div", { class: "banner" }, h("p", { text: `${plural(s.review, "face")} need a look. The AI wasn't sure about them.` }),
      h("a", { class: "btn primary", href: "#/review", text: "Start review" })));
  }
  if (s.ungrouped > 0) {
    out.push(h("div", { class: "banner" }, h("p", { text: `${plural(s.ungrouped, "face")} haven't been grouped yet.` }),
      h("button", { class: "btn", type: "button", text: "Place new faces", onclick: regroup })));
  }
  return out;
}

function noFacesHelp() {
  const s = state.summary || {};
  if (!s.faces) {
    return [h("h2", { text: "No faces found yet" }),
      h("p", {}, "Scan your photos first: ", h("code", { text: "python sort_photos.py faces --apply" }), " then ", h("code", { text: "python sort_photos.py cluster --apply" }), ".")];
  }
  return [h("h2", { text: "No people yet" }), h("p", { text: "The faces haven't been grouped. Choose Place new faces in the left column." })];
}

async function viewPeople() {
  const token = ++renderToken;
  const f = state.filter;
  let offset = 0;
  const grid = h("div", { class: "people" });
  const more = h("button", { class: "btn", type: "button", text: "Show more", hidden: true });
  const empty = h("div", { class: "empty", hidden: true });
  const count = h("span", { class: "count" });

  const seg = h("div", { class: "seg", role: "group", "aria-label": "Show" },
    ...[["all", "All"], ["unnamed", "Unnamed"], ["named", "Named"]].map(([value, label]) =>
      h("button", { type: "button", text: label, "aria-pressed": String(f.named === value), onclick: () => { f.named = value; viewPeople(); } })));
  const search = h("input", { class: "field", type: "search", value: f.q, placeholder: "Search by name", "aria-label": "Search by name" });
  let timer;
  search.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(() => { f.q = search.value.trim(); load(true); }, 250); });
  const sort = h("select", { class: "field", "aria-label": "Sort people", onchange: (e) => { f.sort = e.target.value; load(true); } },
    ...[["size", "Most faces first"], ["review", "Most to review first"], ["name", "Name A to Z"], ["recent", "Newest first"]].map(([value, label]) => h("option", { value, text: label, selected: f.sort === value })));

  main.replaceChildren(
    head("People", "Each strip is one person as the AI grouped them. Open a strip to check it, and type a name under it.", count),
    ...banners(), h("div", { class: "toolbar" }, seg, search, sort), grid, h("p", {}, more), empty);

  state.actions = null; renderSelbar();
  state.reload = () => load(true);

  async function load(reset) {
    if (reset) { offset = 0; grid.replaceChildren(); }
    const data = await api(`/api/people?${new URLSearchParams({ named: f.named, q: f.q, sort: f.sort, limit: 60, offset })}`);
    if (token !== renderToken) return;
    for (const person of data.people) grid.append(personCard(person));
    offset += data.people.length;
    more.hidden = offset >= data.total;
    count.textContent = plural(data.total, "person").replace("persons", "people");
    const filtered = f.q || f.named !== "all";
    empty.hidden = data.total > 0;
    empty.replaceChildren(...(filtered ? [h("h2", { text: "No one matches" }), h("p", { text: "Try another name or choose All." })] : noFacesHelp()));
  }
  more.addEventListener("click", () => load(false).catch((e) => toast(e.message, "error")));
  await load(true).catch((e) => { main.replaceChildren(h("div", { class: "empty" }, h("h2", { text: "Couldn't load people" }), h("p", { text: e.message }))); });
}

function personCard(person) {
  const cells = [];
  for (let i = 0; i < 4; i++) {
    const id = person.samples[i];
    cells.push(h("span", { class: "cell" }, id ? h("img", { src: thumb(id), alt: "", loading: "lazy" }) : null));
  }
  return h("article", { class: "strip-card" },
    h("button", { class: "strip", type: "button", "aria-label": `Open ${personLabel(person)}`, onclick: () => { location.hash = `#/people/${person.id}`; } }, ...cells),
    nameField(person, { onMerged: () => viewPeople() }),
    h("div", { class: "meta" }, h("span", { text: plural(person.faces, "face") }),
      person.review ? h("span", { class: "flag", text: `${person.review} to review` }) : null),
  );
}

async function viewPerson(id) {
  const token = ++renderToken;
  let person;
  try { person = await api(`/api/people/${id}`); }
  catch (e) { toast(e.message, "error"); location.hash = "#/people"; return; }
  if (token !== renderToken) return;

  const order = h("select", { class: "field", "aria-label": "Order of faces", onchange: () => { query.order = order.value; grid.load(true); } },
    h("option", { value: "worst", text: "Weakest matches first" }), h("option", { value: "best", text: "Strongest matches first" }), h("option", { value: "date", text: "Oldest photo first" }));
  const query = { scope: "members", person_id: String(id), order: "worst" };
  const meta = h("p", {});
  const grid = faceGrid(query, { onTotal: (total) => {
    meta.replaceChildren(plural(total, "face"), person.confirmed ? ` · ${person.confirmed} confirmed by you` : "");
  }, empty: h("p", { text: "No faces are in this person any more." }) });
  const similar = h("div", {});

  const mergeBtn = h("button", { class: "btn", type: "button", text: "Merge into another person…", onclick: async () => {
    const { people } = await api(`/api/people/${id}/similar`).catch(() => ({ people: [] }));
    openPicker({ title: `Merge ${personLabel(person)} into…`, text: "All of this person's faces move to the person you choose.", suggestions: people, exclude: id,
      onPick: async ({ person: target }) => {
        const yes = await ask({ title: `Merge into ${personLabel(target)}?`, text: "The two groups become one person. This can't be undone, but you can move faces out again.", confirmLabel: "Merge" });
        if (!yes) return;
        try { await api(`/api/people/${id}/merge`, { body: { into: target.id } }); toast(`Merged into ${personLabel(target)}`); await refreshSummary(); location.hash = `#/people/${target.id}`; }
        catch (e) { toast(e.message, "error"); }
      } });
  } });

  main.replaceChildren(
    h("p", {}, h("a", { class: "back", href: "#/people", text: "← People" })),
    h("div", { class: "page-head" }, h("div", { style: "flex:1;min-width:260px" }, nameField(person, { big: true, onMerged: (into) => { location.hash = `#/people/${into}`; } }), meta),
      h("a", { class: "btn", href: `#/photos?person=${id}`, text: `View photos of ${person.name || "this person"}` }),
      person.review ? h("a", { class: "btn", href: `#/review/${id}`, text: `${plural(person.review, "face")} to review` }) : null, mergeBtn),
    similar, h("div", { class: "toolbar" }, order), grid.el);

  state.actions = () => {
    const unconfirmed = selectedIds().some((fid) => { const f = state.faces.find((x) => x.id === fid); return f && f.status !== "confirmed"; });
    return [
      unconfirmed ? h("button", { class: "btn", type: "button", text: "Confirm they're right", onclick: () => changeFaces("/api/faces/confirm", selectedIds(), (r) => `Confirmed ${plural(r.changed, "face")}`) }) : null,
      h("button", { class: "btn primary", type: "button", text: "Move to…", onclick: () => moveTo(id) }),
      h("button", { class: "btn danger", type: "button", text: `Not ${person.name || "this person"}`, title: "Send these faces back to the review queue and never suggest this person for them again",
        onclick: () => changeFaces("/api/faces/reject", selectedIds(), (r) => `Sent ${plural(r.changed, "face")} to review`) }),
      ignoreAction(),
    ];
  };
  state.reload = async () => {
    const fresh = await api(`/api/people/${id}`).catch(() => null);
    if (!fresh) { location.hash = "#/people"; return; }
    person = fresh;
    await grid.load(true);
  };
  await grid.load(true).catch((e) => toast(e.message, "error"));

  api(`/api/people/${id}/similar`).then(({ people }) => {
    if (token !== renderToken || !people.length) return;
    similar.replaceChildren(h("h2", { class: "sub", text: "Might be the same person" }),
      h("div", { class: "similar" }, ...people.slice(0, 5).map((p) => h("div", { class: "similar-item" },
        p.samples[0] ? h("img", { src: thumb(p.samples[0]), alt: "" }) : h("span"),
        h("div", {}, h("div", { class: "t", text: personLabel(p) }), h("div", { class: "m", text: `${plural(p.faces, "face")} · match ${p.similarity.toFixed(2)}` }),
          h("button", { class: "btn quiet", type: "button", text: "Add to " + (person.name || "this person"), onclick: async () => {
            const yes = await ask({ title: `Add ${personLabel(p)} to ${personLabel(person)}?`, text: `${plural(p.faces, "face")} will join this person${person.name ? "" : " and take over the name " + (p.name || "(none yet)")}.`, confirmLabel: "Merge" });
            if (!yes) return;
            try { await api(`/api/people/${p.id}/merge`, { body: { into: id } }); toast("Merged"); await refreshSummary(); viewPerson(id); }
            catch (e) { toast(e.message, "error"); }
          } }))))));
  }).catch(() => {});
}

async function viewReview(arg) {
  const token = ++renderToken;
  let data;
  try { data = await api("/api/review/buckets"); } catch (e) { toast(e.message, "error"); return; }
  if (token !== renderToken) return;
  if (!data.buckets.length) {
    main.replaceChildren(head("Review", "Faces the AI wasn't sure about show up here."),
      h("div", { class: "empty" }, h("h2", { text: "Nothing to review" }), h("p", { text: "Every face is either matched with confidence or already decided by you. After a new scan, run the grouping to see new questions." })));
    state.actions = null; state.reload = () => viewReview(); renderSelbar(); return;
  }
  const key = (b) => (b.person_id == null ? "none" : String(b.person_id));
  const bucket = data.buckets.find((b) => key(b) === arg) || data.buckets[0];

  const list = h("div", { class: "buckets", role: "list" }, ...data.buckets.map((b) =>
    h("button", { class: "bucket", type: "button", role: "listitem", "aria-current": String(b === bucket), onclick: () => { location.hash = `#/review/${key(b)}`; } },
      b.sample ? h("img", { src: thumb(b.sample), alt: "" }) : h("span", { class: "blank" }),
      h("span", { class: "t" + (b.name ? "" : " unnamed"), text: b.person_id == null ? "No suggestion" : (b.name || "Unnamed person") }),
      h("span", { class: "count", text: b.count.toLocaleString() }))));

  const total = h("span", { class: "count" });
  const grid = faceGrid({ scope: "review", person_id: key(bucket), order: "best" }, { onTotal: (n) => { total.textContent = plural(n, "face"); } });
  let titleBlock;
  let nameRow = null;
  if (bucket.person_id == null) {
    titleBlock = head("No suggestion", "The AI couldn't match these faces to anyone. Select the ones that belong together and move them to a person.", total);
  } else {
    const person = await api(`/api/people/${bucket.person_id}`).catch(() => null);
    if (token !== renderToken) return;
    titleBlock = head(bucket.name ? `Is this ${bucket.name}?` : "Do these faces belong to one person?",
      "The AI thinks these faces match this person but isn't sure. Select the ones that do, then confirm them.", total);
    if (person) {
      nameRow = h("div", { class: "toolbar" }, h("span", { class: "count", text: "Person:" }), nameField(person),
        h("a", { href: `#/people/${bucket.person_id}`, text: "Open this person" }));
    }
  }
  main.replaceChildren(h("div", { class: "split" }, list, h("div", {}, titleBlock, nameRow, grid.el)));
  const label = bucket.name || "this person";
  state.actions = () => [
    bucket.person_id != null ? h("button", { class: "btn primary", type: "button", text: `Confirm as ${label}`, onclick: () => changeFaces("/api/faces/confirm", selectedIds(), (r) => `Confirmed ${plural(r.changed, "face")} as ${label}`) }) : null,
    h("button", { class: bucket.person_id == null ? "btn primary" : "btn", type: "button", text: "Move to…", onclick: () => moveTo(bucket.person_id) }),
    bucket.person_id != null ? h("button", { class: "btn danger", type: "button", text: `Not ${label}`, onclick: () => changeFaces("/api/faces/reject", selectedIds(), (r) => `Marked ${plural(r.changed, "face")} as not ${label}`) }) : null,
    ignoreAction(),
  ];
  state.reload = () => viewReview(arg);
  await grid.load(true).catch((e) => toast(e.message, "error"));
}

async function viewScope(scope) {
  const token = ++renderToken;
  const isIgnored = scope === "ignored";
  const query = { scope, order: "best" };
  const total = h("span", { class: "count" });
  const grid = faceGrid(query, {
    onTotal: (n) => { total.textContent = plural(n, "face"); },
    empty: isIgnored ? h("p", { text: "You haven't ignored any faces." }) : h("p", { text: "No loose faces. Everything the AI found is in a group or waiting in Review." }),
  });
  const filters = [];
  if (!isIgnored) {
    const seg = h("div", { class: "seg", role: "group", "aria-label": "Why unassigned" },
      ...[["", "All"], ["no_match", "No match"], ["low_quality", "Hard to see"]].map(([value, label]) =>
        h("button", { type: "button", text: label, "aria-pressed": String((query.reason || "") === value), onclick: () => { if (value) query.reason = value; else delete query.reason; viewScope(scope); } })));
    filters.push(h("div", { class: "toolbar" }, seg));
  }
  main.replaceChildren(
    head(isIgnored ? "Ignored" : "Unassigned",
      isIgnored ? "Faces you dismissed. The AI never places them again unless you restore them." : "Faces with no good match, usually strangers in the background. Move the ones that matter to a person.", total),
    ...filters, grid.el);
  state.actions = () => isIgnored
    ? [h("button", { class: "btn primary", type: "button", text: "Restore to review", onclick: () => changeFaces("/api/faces/restore", selectedIds(), (r) => `Restored ${plural(r.changed, "face")}`) })]
    : [h("button", { class: "btn primary", type: "button", text: "Move to…", onclick: () => moveTo() }), ignoreAction()];
  state.reload = () => viewScope(scope);
  if (token === renderToken) await grid.load(true).catch((e) => toast(e.message, "error"));
}

// ---------------------------------------------------------------- photos (browse the whole library)
const MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];

function photosHash(f) {
  const p = new URLSearchParams();
  if (f.person.length) p.set("person", f.person.join(","));
  if (f.person.length > 1 && f.match === "any") p.set("match", "any");
  if (f.person.length && f.uncertain) p.set("uncertain", "1");
  if (f.filter) p.set("filter", f.filter);
  if (f.year) p.set("year", f.year);
  if (f.year && f.month) p.set("month", f.month);
  if (f.q) p.set("q", f.q);
  if (f.order === "old") p.set("order", "old");
  const text = p.toString();
  return "#/photos" + (text ? "?" + text : "");
}

function monthHeading(taken) {
  if (!taken) return "Undated";
  const [year, month] = taken.split("-");
  return `${MONTHS[Number(month) - 1] || ""} ${year}`.trim();
}

const photoSel = new Set();   // ids of photos ticked in the Photos view
let lastPicked = null;

function photoTile(photo) {
  const names = photo.people.map((p) => p.name);
  const who = names.length ? names.slice(0, 3).join(", ") + (names.length > 3 ? ` +${names.length - 3}` : "") : "";
  const open = (e) => {
    if (photoSel.size) togglePhoto(photo, e.shiftKey);
    else openViewer(state.photoList.indexOf(photo));
  };
  return h("div", { class: "photo", "data-id": String(photo.id) },
    h("button", { class: "pbody", type: "button", "aria-label": `${photo.name}, ${photo.taken || "undated"}`, onclick: open },
      h("img", { src: `/api/photos/${photo.id}/thumb`, alt: "", loading: "lazy", onerror: (e) => e.target.closest(".photo").classList.add("is-missing") }),
      h("span", { class: "pcap" },
        h("span", { class: "who" + (who ? "" : " none"), text: who || (photo.faces ? "No one named yet" : photo.name) }),
        h("span", { class: "when" }, photo.taken || "", photo.unlabeled > 0 && photo.faces ? h("span", { class: "flag", text: `  ${photo.unlabeled} unlabeled` }) : null))),
    h("button", { class: "ptick", type: "button", "aria-pressed": "false", "aria-label": `Select ${photo.name}`, title: "Select (Shift-click selects a range)",
      onclick: (e) => togglePhoto(photo, e.shiftKey) }));
}

function togglePhoto(photo, range) {
  const list = state.photoList;
  if (range && lastPicked != null) {
    const from = list.findIndex((p) => p.id === lastPicked), to = list.indexOf(photo);
    if (from >= 0 && to >= 0) for (let i = Math.min(from, to); i <= Math.max(from, to); i++) photoSel.add(list[i].id);
  } else if (photoSel.has(photo.id)) photoSel.delete(photo.id);
  else photoSel.add(photo.id);
  lastPicked = photo.id;
  syncPhotoTiles(); renderPhotoSelbar();
}

function syncPhotoTiles() {
  const grid = document.querySelector(".photos");
  if (grid) grid.classList.toggle("selecting", photoSel.size > 0);
  for (const tile of document.querySelectorAll(".photos .photo")) {
    const on = photoSel.has(Number(tile.dataset.id));
    tile.classList.toggle("is-selected", on);
    tile.querySelector(".ptick").setAttribute("aria-pressed", String(on));
  }
}

function clearPhotoSelection() { photoSel.clear(); lastPicked = null; syncPhotoTiles(); renderPhotoSelbar(); }

function renderPhotoSelbar() {
  if (!photoSel.size) { selbar.hidden = true; return; }
  selbar.hidden = false;
  selbar.replaceChildren(
    h("span", { class: "n", text: `${photoSel.size.toLocaleString()} selected` }),
    h("button", { class: "btn quiet", type: "button", text: "Select all shown", onclick: () => { state.photoList.forEach((p) => photoSel.add(p.id)); syncPhotoTiles(); renderPhotoSelbar(); } }),
    h("button", { class: "btn quiet", type: "button", text: "Clear", onclick: clearPhotoSelection }),
    h("span", { class: "spacer" }),
    h("button", { class: "btn danger", type: "button", text: photoSel.size === 1 ? "Delete photo" : `Delete ${photoSel.size.toLocaleString()} photos`, onclick: deleteSelectedPhotos }));
}

/** Drop photos from the list and the grid after they were deleted, without reloading the page. */
function removePhotos(ids) {
  const gone = new Set(ids);
  state.photoList = state.photoList.filter((p) => !gone.has(p.id));
  for (const tile of document.querySelectorAll(".photos .photo")) if (gone.has(Number(tile.dataset.id))) tile.remove();
  for (const heading of document.querySelectorAll(".photos h2.month")) {
    const next = heading.nextElementSibling;
    if (!next || !next.classList.contains("photo")) heading.remove();
  }
  ids.forEach((id) => photoSel.delete(id));
  if (state.onRemoved) state.onRemoved(ids.length);
  refreshSummary().catch(() => {});
  renderPhotoSelbar();
}

function deleteSelectedPhotos() {
  const chosen = state.photoList.filter((p) => photoSel.has(p.id));
  if (!chosen.length) return;
  const n = chosen.length;
  const label = n === 1 ? "photo" : `${n.toLocaleString()} photos`;
  const cancel = h("button", { class: "btn", type: "button", text: "Cancel" });
  const ok = h("button", { class: "btn danger", type: "button", text: n === 1 ? "Delete photo" : `Delete ${n.toLocaleString()} photos` });
  const dialog = openDialog(h("div", { class: "dlg" },
    h("h2", { text: `Delete ${label}?` }),
    h("div", { class: "thumbs" },
      ...chosen.slice(0, 8).map((p) => h("img", { src: `/api/photos/${p.id}/thumb`, alt: p.name, title: p.name })),
      n > 8 ? h("span", { class: "more", text: `+${(n - 8).toLocaleString()} more` }) : null),
    h("p", { text: `${n === 1 ? chosen[0].name + " will" : label + " will"} be moved to the ${TRASH_FOLDER} folder in your library and removed from the Photo sorter, along with their face matches. Nothing is erased from your drive, so you can move them back yourself.` }),
    h("div", { class: "row" }, cancel, ok)));
  cancel.addEventListener("click", () => dialog.close());
  ok.addEventListener("click", async () => {
    ok.disabled = cancel.disabled = true; ok.textContent = "Deleting…";
    try {
      const result = await api("/api/photos/delete", { body: { photo_ids: chosen.map((p) => p.id) } });
      dialog.close();
      removePhotos(result.deleted);
      if (result.failed.length) toast(`${plural(result.deleted.length, "photo")} deleted. ${result.failed.length} could not be deleted: ${result.failed[0].error}`, "error");
      else toast(`Deleted ${plural(result.deleted.length, "photo")}`);
    } catch (e) { dialog.close(); toast(e.message, "error"); }
  });
  cancel.focus();
}

async function viewPhotos(params) {
  const token = ++renderToken;
  const f = {
    person: (params.get("person") || "").split(",").filter(Boolean).map(Number),
    match: params.get("match") === "any" ? "any" : "all",
    uncertain: params.get("uncertain") === "1",
    filter: params.get("filter") || "", year: params.get("year") || "", month: params.get("month") || "",
    q: params.get("q") || "", order: params.get("order") === "old" ? "old" : "new",
  };
  const go = (changes) => { location.hash = photosHash({ ...f, ...changes }); };
  const filtered = f.person.length || f.filter || f.year || f.q;

  let lib, chips;
  try { [lib, chips] = await Promise.all([api("/api/library"), api("/api/people/chips")]); }
  catch (e) { main.replaceChildren(h("div", { class: "empty" }, h("h2", { text: "Couldn't load the library" }), h("p", { text: e.message }))); return; }
  if (token !== renderToken) return;

  // quick filters
  const quick = [["", "All photos", lib.total], ["unlabeled", "Unlabeled faces", lib.counts.unlabeled], ["review", "Needs review", lib.counts.review],
    ["noface", "No faces", lib.counts.noface], ["unscanned", "Not scanned yet", lib.counts.unscanned], ["notes", "Has notes", lib.counts.notes || 0]]
    .filter(([value, , count]) => value === "" || value === f.filter || count > 0);
  const seg = h("div", { class: "seg", role: "group", "aria-label": "Quick filters" }, ...quick.map(([value, label, count]) =>
    h("button", { type: "button", "aria-pressed": String(f.filter === value), onclick: () => go({ filter: value }) }, label, " ", h("span", { class: "count", text: count.toLocaleString() }))));

  // people chips: the most photographed named people, plus anyone selected
  const shown = chips.people.slice(0, 24);
  const known = new Map(chips.people.map((p) => [p.id, p]));
  for (const id of f.person) {
    if (!known.has(id)) {
      const extra = await api(`/api/people/${id}`).catch(() => null);
      if (extra) { const chip = { id, name: extra.name, photos: null, sample: extra.samples[0] }; known.set(id, chip); shown.push(chip); }
    } else if (!shown.includes(known.get(id))) shown.push(known.get(id));
  }
  const chipButton = (p) => {
    const on = f.person.includes(p.id);
    return h("button", { class: "chip", type: "button", "aria-pressed": String(on), title: on ? "Remove from the filter" : "Show photos with this person",
      onclick: () => go({ person: on ? f.person.filter((x) => x !== p.id) : [...f.person, p.id] }) },
      p.sample ? h("img", { src: thumb(p.sample), alt: "" }) : h("span", { class: "dot" }),
      h("span", { class: "n", text: p.name || "Unnamed person" }), p.photos != null ? h("span", { class: "count", text: p.photos.toLocaleString() }) : null);
  };
  const peopleRow = h("div", { class: "chips", role: "group", "aria-label": "People" },
    ...shown.map(chipButton),
    chips.people.length > 24 ? h("button", { class: "btn quiet", type: "button", text: "Find someone…", onclick: () => openPicker({ title: "Filter by person", onPick: ({ person }) => go({ person: [...new Set([...f.person, person.id])] }) }) }) : null,
    !chips.people.length ? h("span", { class: "count", text: chips.unnamed_groups ? "Name people on the People page and they appear here as quick links." : "No people found yet." }) : null);

  const options = [];
  if (f.person.length > 1) {
    options.push(h("div", { class: "seg", role: "group", "aria-label": "Match" },
      ...[["all", "In the same photo"], ["any", "Any of them"]].map(([value, label]) => h("button", { type: "button", "aria-pressed": String(f.match === value), onclick: () => go({ match: value }) }, label))));
  }
  if (f.person.length) {
    const box = h("input", { type: "checkbox", checked: f.uncertain, onchange: () => go({ uncertain: box.checked }) });
    options.push(h("label", { class: "check" }, box, " Include matches the AI is unsure about"));
  }

  // date, search, order
  const yearSel = h("select", { class: "field", "aria-label": "Year", onchange: (e) => go({ year: e.target.value, month: "" }) },
    h("option", { value: "", text: "All years" }),
    ...lib.years.map((y) => h("option", { value: y.year, selected: f.year === y.year, text: `${y.year} (${y.count.toLocaleString()})` })));
  const months = (lib.years.find((y) => y.year === f.year) || { months: [] }).months;
  const monthSel = h("select", { class: "field", "aria-label": "Month", disabled: !f.year, onchange: (e) => go({ month: e.target.value }) },
    h("option", { value: "", text: "All months" }),
    ...months.slice().sort((a, b) => a.month.localeCompare(b.month)).map((m) => h("option", { value: m.month, selected: f.month === m.month, text: `${MONTHS[Number(m.month) - 1] || m.month} (${m.count.toLocaleString()})` })));
  const search = h("input", { class: "field", type: "search", value: f.q, placeholder: "Name, folder or note", "aria-label": "Search file names, folders and notes", onchange: (e) => go({ q: e.target.value.trim() }) });
  const orderSel = h("select", { class: "field", "aria-label": "Order", onchange: (e) => go({ order: e.target.value }) },
    h("option", { value: "new", text: "Newest first", selected: f.order === "new" }), h("option", { value: "old", text: "Oldest first", selected: f.order === "old" }));

  const total = h("span", { class: "count" });
  const grid = h("div", { class: "photos" });
  const more = h("button", { class: "btn", type: "button", text: "Show more", hidden: true });
  const empty = h("div", { class: "empty", hidden: true });
  main.replaceChildren(...[
    head("Photos", "Everything in your library, newest first. Pick people to see the photos they are in. Tick photos to delete several at once.", total),
    h("div", { class: "toolbar" }, seg),
    peopleRow, options.length ? h("div", { class: "toolbar", style: "margin-top:12px" }, ...options) : null,
    h("div", { class: "toolbar", style: "margin-top:14px" }, yearSel, monthSel, search, orderSel,
      filtered ? h("button", { class: "btn quiet", type: "button", text: "Clear filters", onclick: () => { location.hash = "#/photos"; } }) : null),
    grid, h("p", {}, more), empty].filter(Boolean));

  state.actions = null; renderSelbar();
  state.photoList = [];
  photoSel.clear(); lastPicked = null;
  let offset = 0;
  let shownTotal = 0;
  let lastMonth = null;
  async function load() {
    const query = new URLSearchParams({ person: f.person.join(","), match: f.match, uncertain: String(f.uncertain), filter: f.filter, year: f.year, month: f.month, q: f.q, order: f.order, limit: 96, offset });
    const data = await api(`/api/photos?${query}`);
    if (token !== renderToken) return 0;
    for (const photo of data.photos) {
      const key = (photo.taken || "").slice(0, 7);
      if (key !== lastMonth) { lastMonth = key; grid.append(h("h2", { class: "month", text: monthHeading(photo.taken) })); }
      state.photoList.push(photo);
      grid.append(photoTile(photo));
    }
    offset += data.photos.length;
    more.hidden = offset >= data.total;
    shownTotal = data.total;
    total.textContent = plural(data.total, "photo");
    empty.hidden = data.total > 0;
    if (!data.total) {
      empty.replaceChildren(...(filtered
        ? [h("h2", { text: "No photos match" }), h("p", { text: "Try removing a filter. With several people selected, photos must contain all of them unless you choose Any of them." })]
        : [h("h2", { text: "No photos in the index yet" }), h("p", {}, "Run ", h("code", { text: "python sort_photos.py sort SOURCE DEST --apply" }), " or ", h("code", { text: "refresh DEST --apply" }), " first.")]));
    }
    return data.photos.length;
  }
  state.onRemoved = (n = 1) => { offset -= n; shownTotal -= n; total.textContent = plural(shownTotal, "photo"); empty.hidden = shownTotal > 0; if (!shownTotal) empty.replaceChildren(h("h2", { text: "No photos left here" })); };
  state.loadMorePhotos = async () => (more.hidden ? 0 : load());
  more.addEventListener("click", () => load().catch((e) => toast(e.message, "error")));
  state.reload = () => viewPhotos(params);
  await load().catch((e) => toast(e.message, "error"));
}

const TRASH_FOLDER = ".photo-sorter-trash";
let viewerToken = 0;
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && photoSel.size && !document.querySelector("dialog[open]")) clearPhotoSelection();
});
async function openViewer(start) {
  let index = start;
  let imageUrl = null;
  const frame = h("div", { class: "stage" });
  const side = h("div", { class: "vside" });
  const position = h("span", { class: "count" });
  const prev = h("button", { class: "btn", type: "button", text: "← Previous", onclick: () => step(-1) });
  const next = h("button", { class: "btn", type: "button", text: "Next →", onclick: () => step(1) });
  const close = h("button", { class: "btn", type: "button", text: "Close", onclick: () => dialog.close() });
  const dialog = openDialog(h("div", { class: "viewer" }, frame, side,
    h("div", { class: "vbar" }, prev, next, position, h("span", { class: "spacer" }), close)), { wide: true,
    onClose: () => { document.removeEventListener("keydown", onKey); if (imageUrl) URL.revokeObjectURL(imageUrl); saveNote(); } });
  dialog.classList.add("viewer-dialog");
  // listen on the document: focus falls back to <body> whenever the photo is swapped
  const onKey = (e) => {
    if (e.target && /^(INPUT|SELECT|TEXTAREA)$/.test(e.target.tagName)) return;
    if (document.querySelectorAll("dialog[open]").length > 1) return;   // a confirmation is open on top
    if (e.key === "ArrowLeft") step(-1); else if (e.key === "ArrowRight") step(1);
  };
  document.addEventListener("keydown", onKey);

  let saveNote = () => {};   // saves the note being typed (set by show); called before leaving a photo

  async function step(delta) {
    saveNote();
    const target = index + delta;
    if (target < 0) return;
    if (target >= state.photoList.length) { if (!(state.loadMorePhotos && await state.loadMorePhotos()) || target >= state.photoList.length) return; }
    show(target);
  }

  function renamePhoto(photo, detail) {
    const dot = detail.name.lastIndexOf(".");
    const ext = dot > 0 ? detail.name.slice(dot) : "";
    const stem = dot > 0 ? detail.name.slice(0, dot) : detail.name;
    const input = h("input", { class: "field", type: "text", value: stem, "aria-label": "New file name", maxlength: "150", spellcheck: "false" });
    const preview = h("p", { class: "count" });
    const problem = h("p", { class: "err", hidden: true });
    const ok = h("button", { class: "btn primary", type: "button", text: "Rename" });
    const cancel = h("button", { class: "btn", type: "button", text: "Cancel" });
    const typed = () => { const t = input.value.trim(); return t.toLowerCase().endsWith(ext.toLowerCase()) && ext ? t : t + ext; };
    const refresh = () => { preview.replaceChildren("Will be saved as ", h("strong", { text: typed() })); problem.hidden = true; ok.disabled = !input.value.trim(); };
    input.addEventListener("input", refresh); refresh();
    const confirm = openDialog(h("div", { class: "dlg" },
      h("h2", { text: "Rename this photo?" }),
      h("p", {}, "The file on your drive is renamed. It stays in the same folder and keeps its ", h("strong", { text: ext || "type" }), " ending."),
      h("p", { class: "count" }, "Now: ", detail.name), input, preview, problem,
      h("div", { class: "row" }, cancel, ok)));
    const submit = async () => {
      if (ok.disabled) return;
      ok.disabled = true;
      try {
        const done = await api(`/api/photos/${photo.id}/rename`, { body: { name: input.value } });
        confirm.close();
        photo.name = done.name;
        const tile = document.querySelectorAll(".photos .photo")[index];
        if (tile && !photo.faces) { const who = tile.querySelector(".who"); if (who) who.textContent = done.name; }
        if (tile) tile.setAttribute("aria-label", `${done.name}, ${photo.taken || "undated"}`);
        toast(done.changed ? `Renamed to ${done.name}` : "The name was not changed");
        show(index);
      } catch (e) { problem.textContent = e.message; problem.hidden = false; ok.disabled = false; input.focus(); }
    };
    ok.addEventListener("click", submit);
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); submit(); } });
    cancel.addEventListener("click", () => confirm.close());
    input.focus(); input.select();
  }

  async function deletePhoto(photo, detail) {
    const yes = await ask({
      title: "Delete this photo?",
      text: `${detail.name} will be moved to the ${TRASH_FOLDER} folder in your library and removed from the Photo sorter, along with its face matches. It is not erased from your drive, so you can move it back yourself.`,
      confirmLabel: "Delete photo", danger: true });
    if (!yes) return;
    try { await api(`/api/photos/${photo.id}/delete`, { body: {} }); }
    catch (e) { toast(e.message, "error"); return; }
    removePhotos([photo.id]);
    toast(`Deleted ${detail.name}`);
    if (!state.photoList.length) { dialog.close(); return; }
    show(Math.min(index, state.photoList.length - 1));
  }

  function cameraBlock(camera) {
    if (!camera) return h("p", { class: "vcam count", text: "Camera details not read yet (they are read by the next refresh)." });
    if (camera.error) return h("p", { class: "vcam count", text: "Camera details could not be read from this file." });
    const lines = [];
    const body = [camera.camera, camera.lens].filter(Boolean).join(" · ");
    if (body) lines.push(h("div", { class: "t", text: body }));
    if (camera.settings) lines.push(h("div", { text: camera.settings }));
    if (camera.width && camera.height) lines.push(h("div", { text: `${camera.width.toLocaleString()} × ${camera.height.toLocaleString()} pixels` }));
    if (camera.latitude != null && camera.longitude != null) {
      const alt = camera.altitude_m != null ? `, ${Math.round(camera.altitude_m)} m` : "";
      lines.push(h("div", { class: "vgps", text: `Location ${camera.latitude.toFixed(5)}, ${camera.longitude.toFixed(5)}${alt}` }));
    }
    if (!lines.length) lines.push(h("div", { class: "count", text: "No camera details in this file." }));
    return h("div", { class: "vcam" }, ...lines);
  }

  function annotationBlock(items) {
    if (!items.length) return [];
    const byKind = new Map();
    for (const a of items) { if (!byKind.has(a.kind)) byKind.set(a.kind, []); byKind.get(a.kind).push(a); }
    const titles = { tag: "Tags", caption: "Description", ocr: "Text in the photo", note: "Notes" };
    const out = [];
    for (const [kind, list] of byKind) {
      out.push(h("h3", { text: titles[kind] || kind }));
      if (kind === "tag") out.push(h("p", { class: "vtags" }, ...list.map((a) => h("span", { class: "chip tag", text: a.text, title: a.source }))));
      else for (const a of list) out.push(h("p", { class: "vnote-text", text: a.text, title: a.source }));
    }
    return out;
  }

  function noteBlock(photo, detail) {
    let saved = detail.note || "";
    const status = h("span", { class: "count", "aria-live": "polite" });
    const box = h("textarea", { class: "field vnote", rows: "3", maxlength: "4000", placeholder: "Add a note: who, where, what happened. You can search for it on the Photos page.", "aria-label": "Your note on this photo" });
    box.value = saved;
    const mine = viewerToken;
    async function save() {
      const text = box.value;
      if (text.trim() === saved.trim()) return;
      status.textContent = "Saving…";
      try {
        const result = await api(`/api/photos/${photo.id}/note`, { body: { text } });
        saved = result.note;
        if (mine === viewerToken) { box.value = saved; status.textContent = saved ? "Saved" : "Note removed"; }
        else toast(saved ? "Note saved" : "Note removed");
      } catch (e) { status.textContent = ""; toast(e.message, "error"); }
    }
    saveNote = save;
    box.addEventListener("blur", save);
    box.addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); save(); } });
    box.addEventListener("input", () => { status.textContent = box.value.trim() === saved.trim() ? "" : "Not saved yet"; });
    return h("div", { class: "vnotes" }, h("h3", { text: "Your note" }), box,
      h("div", { class: "row" }, status, h("span", { class: "spacer" }), h("button", { class: "btn quiet", type: "button", text: "Save note", onclick: save })));
  }

  async function show(n) {
    saveNote = () => {};
    index = n;
    const mine = ++viewerToken;
    const photo = state.photoList[index];
    position.textContent = `${index + 1} of ${state.photoList.length}`;
    prev.disabled = index === 0;
    frame.replaceChildren(h("div", { class: "loading", text: "Opening the photo…" }));
    side.replaceChildren();
    let detail, response;
    try { [detail, response] = await Promise.all([api(`/api/photos/${photo.id}`), fetch(`/api/photos/${photo.id}/image`)]); }
    catch (e) { if (mine === viewerToken) frame.replaceChildren(h("div", { class: "loading", text: e.message })); return; }
    if (mine !== viewerToken) return;
    if (!response.ok) {
      let message = "The photo could not be opened.";
      try { message = (await response.json()).detail || message; } catch { /* keep default */ }
      frame.replaceChildren(h("div", { class: "loading", text: message }));
    } else {
      if (imageUrl) URL.revokeObjectURL(imageUrl);
      imageUrl = URL.createObjectURL(await response.blob());
      if (mine !== viewerToken) return;
      const boxes = detail.faces.filter((fc) => fc.status !== "ignored").map((fc) => {
        const [x1, y1, x2, y2] = fc.box;
        const named = fc.name && (fc.status === "auto" || fc.status === "confirmed");
        const label = named ? fc.name : (fc.status === "review" && fc.name ? `${fc.name}?` : "Not labeled");
        const el = h(named ? "button" : "span", { class: "fbox" + (named ? " named" : ""), type: named ? "button" : false,
          title: named ? `Show photos of ${fc.name}` : "Not labeled yet",
          style: `left:${x1 * 100}%;top:${y1 * 100}%;width:${(x2 - x1) * 100}%;height:${(y2 - y1) * 100}%`,
          onclick: named ? () => { dialog.close(); location.hash = photosHash({ person: [fc.person_id], match: "all", uncertain: false, filter: "", year: "", month: "", q: "", order: "new" }); } : null },
          h("span", { class: "blabel", text: label }));
        return el;
      });
      frame.replaceChildren(h("div", { class: "frame" }, h("img", { src: imageUrl, alt: `${detail.name}`, class: "vimg" }), ...boxes));
    }
    const rows = detail.faces.filter((fc) => fc.status !== "ignored").map((fc) => {
      const named = fc.name && (fc.status === "auto" || fc.status === "confirmed");
      return h("div", { class: "vface" }, h("img", { src: thumb(fc.id), alt: "" }),
        h("div", {}, h("div", { class: "t" + (named ? "" : " unnamed"), text: named ? fc.name : fc.status === "review" && fc.name ? `Maybe ${fc.name}` : "Not labeled yet" }),
          named ? h("div", {}, h("button", { class: "linkbtn", type: "button", text: "Show their photos", onclick: () => { dialog.close(); location.hash = `#/photos?person=${fc.person_id}`; } }),
            " · ", h("a", { href: `#/people/${fc.person_id}`, text: "Open person", onclick: () => dialog.close() }))
            : h("div", {}, h("a", { href: fc.status === "review" ? `#/review${fc.person_id ? "/" + fc.person_id : ""}` : "#/unassigned", text: fc.status === "review" ? "Review it" : "See unassigned faces", onclick: () => dialog.close() }))));
    });
    side.replaceChildren(
      h("h2", { text: detail.name }),
      h("p", { class: "vmeta", text: `${detail.taken_at ? "Taken " + detail.taken_at.replace("T", " ") : "No date"} (${detail.date_source})` }),
      h("p", { class: "vpath", text: detail.path }),
      cameraBlock(detail.camera),
      h("h3", { text: rows.length ? "People in this photo" : (detail.scanned ? "No faces found" : "Faces not scanned yet") }), ...rows,
      ...annotationBlock(detail.annotations || []),
      noteBlock(photo, detail),
      h("div", { class: "vactions" },
        h("button", { class: "btn", type: "button", text: "Rename", onclick: () => renamePhoto(photo, detail) }),
        h("button", { class: "btn danger", type: "button", text: "Delete photo", onclick: () => deletePhoto(photo, detail) })));
  }
  show(index);
}

// ---------------------------------------------------------------- router
function route() {
  photoSel.clear(); lastPicked = null; selbar.hidden = true;
  clearSelection();
  state.actions = null;
  renderRail();
  const [pathPart, queryPart = ""] = location.hash.replace(/^#\/?/, "").split("?");
  const [section, arg] = pathPart.split("/");
  window.scrollTo(0, 0);
  if (section === "photos") return viewPhotos(new URLSearchParams(queryPart));
  if (section === "people" && arg) return viewPerson(Number(arg));
  if (section === "review") return viewReview(arg);
  if (section === "unassigned" || section === "ignored") return viewScope(section);
  return viewPeople();
}

async function init() {
  try { await refreshSummary(); }
  catch (e) { main.replaceChildren(h("div", { class: "empty" }, h("h2", { text: "Can't reach the photo sorter" }), h("p", { text: e.message }))); return; }
  if (!location.hash) location.replace(state.summary.review > 0 ? "#/review" : "#/people");
  window.addEventListener("hashchange", route);
  route();
}
init();
