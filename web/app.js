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

// ---------------------------------------------------------------- router
function route() {
  clearSelection();
  state.actions = null;
  renderRail();
  const [section, arg] = location.hash.replace(/^#\/?/, "").split("/");
  window.scrollTo(0, 0);
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
