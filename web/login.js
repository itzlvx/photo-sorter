"use strict";
// The sign-in page: sign in as admin, or choose the password the first time (when none is set yet).

const $ = (id) => document.getElementById(id);
const form = $("form"), errorBox = $("error"), submit = $("submit");
let setup = false;

function showError(message) {
  errorBox.textContent = message;
  errorBox.hidden = !message;
}

function next() {
  // Back to the page that sent us here (only a #/... place inside this app, never another site).
  // The server's redirect keeps the #/... of the page asked for; the app adds ?next= when a session ends.
  const wanted = new URLSearchParams(location.search).get("next") || location.hash || "";
  return "/" + (/^#\/[\w\-/?=&%.]*$/.test(wanted) ? wanted : "");
}

async function post(path, body) {
  const response = await fetch(path, {
    method: "POST", headers: { "Content-Type": "application/json", "X-Photo-Sorter": "1" }, body: JSON.stringify(body),
  });
  let data = null;
  try { data = await response.json(); } catch { /* not JSON */ }
  if (!response.ok) throw new Error(data && typeof data.detail === "string" ? data.detail : `Something went wrong (error ${response.status}).`);
  return data;
}

async function start() {
  let state;
  try { state = await (await fetch("/api/auth/state")).json(); }
  catch { showError("Can't reach the photo sorter. Check that the window running it is still open."); form.hidden = false; return; }
  if (state.signed_in) { location.replace(next()); return; }
  setup = !state.password_set;
  $("username").value = state.user;
  if (setup) {
    document.title = "Choose a password · Photo sorter";
    $("title").textContent = "Choose a password";
    $("intro").textContent = `No password is set yet. Choose one for the user ${state.user}: you'll need it to open the photo sorter, from this computer and from others.`;
    $("username").readOnly = true;
    $("password").autocomplete = "new-password";
    $("password").minLength = state.min_length;
    $("repeat-field").hidden = false;
    submit.textContent = "Set password and open";
    $("hint").textContent = `At least ${state.min_length} characters. Forgot it later? Reset it with option 7 in photo-sorter.bat on the computer running it.`;
  } else {
    $("intro").textContent = "Sign in to see your photos.";
    $("password").autocomplete = "current-password";
    $("hint").textContent = "Forgot the password? Reset it with option 7 in photo-sorter.bat on the computer running the photo sorter.";
  }
  form.hidden = false;
  (setup || $("username").value ? $("password") : $("username")).focus();
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  showError("");
  const password = $("password").value;
  if (setup && password !== $("repeat").value) { showError("The two passwords are different."); $("repeat").focus(); return; }
  if (!password) { showError("Type the password."); $("password").focus(); return; }
  submit.disabled = true;
  try {
    if (setup) await post("/api/auth/setup", { password });
    else await post("/api/auth/login", { username: $("username").value, password });
    location.replace(next());
  } catch (e) {
    showError(e.message);
    if (/already set|No password/.test(e.message)) setTimeout(() => location.reload(), 1500);
    $("password").select();
  } finally {
    submit.disabled = false;
  }
});

start();
