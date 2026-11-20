"""Signing in to the web interface (web_auth.py): one user, admin; the first visit chooses the password; 'password' resets it."""
import io
import sqlite3
import time

import pytest

import sort_photos as sp
import web_auth
from conftest import scalar

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

HEADER = {"X-Photo-Sorter": "1"}
GOOD = "correct horse"


def login_row(db):
    return web_auth.load(db)


def set_password(db, password=GOOD):
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sp.open_index(db)
    try:
        web_auth.set_password(conn, password)
    finally:
        conn.close()


@pytest.fixture
def server(env):
    """The web interface as 'ui' starts it (sign-in on), without a real network server."""
    import ui_server
    from fastapi.testclient import TestClient
    env.db.parent.mkdir(parents=True, exist_ok=True)
    sp.open_index(env.db).close()
    app = ui_server.create_app(env.db, login=True)
    client = TestClient(app, base_url="http://127.0.0.1:8765")
    yield client
    app.state.jobs.shutdown()


def sign_in(client, username="admin", password=GOOD):
    return client.post("/api/auth/login", json={"username": username, "password": password}, headers=HEADER)


# --------------------------------------------------------------------------- web_auth itself
def test_passwords_are_stored_hashed_and_salted(env):
    set_password(env.db)
    stored = scalar(env.db, "SELECT password FROM web_login WHERE username = 'admin'")
    assert GOOD not in stored and stored.startswith("pbkdf2_sha256$")
    assert web_auth.password_matches(GOOD, stored) and not web_auth.password_matches("correct horsE", stored)
    assert web_auth.hash_password(GOOD) != web_auth.hash_password(GOOD)            # a new salt every time
    assert not web_auth.password_matches(GOOD, "garbage") and not web_auth.password_matches(GOOD, "md5$1$aa$bb")


def test_session_tokens_expire_and_cannot_be_forged(env):
    set_password(env.db)
    row = login_row(env.db)
    token = web_auth.make_token(row)
    assert web_auth.token_ok(row, token)
    assert not web_auth.token_ok(row, token, now=time.time() + 31 * 86400)       # 30 days, then sign in again
    expires, signature = token.split(".")
    assert not web_auth.token_ok(row, f"{int(expires) + 999}.{signature}")        # a longer life can't be made up
    for bad in ("", None, "abc", "123", f"x.{signature}"):
        assert not web_auth.token_ok(row, bad)
    assert not web_auth.token_ok(None, token)


def test_a_new_password_signs_every_browser_out(env):
    set_password(env.db)
    token = web_auth.make_token(login_row(env.db))
    set_password(env.db, "another password")
    assert not web_auth.token_ok(login_row(env.db), token)


@pytest.mark.parametrize("password, problem", [("short", "at least 8"), (" padded password", "space"), ("x" * 201, "at most")])
def test_password_rules(password, problem):
    assert problem in web_auth.problem_with(password)
    assert web_auth.problem_with("eight ch") is None


def test_no_index_or_no_table_means_no_password(env, tmp_path):
    assert web_auth.load(tmp_path / "nothing.db") is None
    sqlite3.connect(tmp_path / "old.db").close()
    assert web_auth.load(tmp_path / "old.db") is None


# --------------------------------------------------------------------------- the web interface
def test_everything_needs_signing_in(server):
    assert server.get("/api/summary").status_code == 401
    assert server.get("/api/photos").status_code == 401
    assert server.post("/api/regroup", json={}, headers=HEADER).status_code == 401
    page = server.get("/", follow_redirects=False)
    assert page.status_code == 303 and page.headers["location"] == "/login"
    assert server.get("/web/style.css").status_code == 200 and server.get("/web/login.js").status_code == 200   # code, no data
    assert "Sign in" in server.get("/login").text


def test_first_visit_chooses_the_password_then_is_signed_in(server, env):
    assert server.get("/api/auth/state").json() == {"user": "admin", "password_set": False, "signed_in": False, "min_length": 8}
    assert sign_in(server).status_code == 409                                   # nothing to sign in with yet
    r = server.post("/api/auth/setup", json={"password": "short"}, headers=HEADER)
    assert r.status_code == 400 and "at least 8" in r.json()["detail"]
    assert server.post("/api/auth/setup", json={"password": GOOD}).status_code == 403     # the request header is required
    r = server.post("/api/auth/setup", json={"password": GOOD}, headers=HEADER)
    assert r.status_code == 200 and web_auth.COOKIE in r.cookies
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert server.get("/api/summary").status_code == 200
    assert server.get("/api/auth/state").json()["signed_in"] is True
    assert server.get("/login", follow_redirects=False).headers["location"] == "/"
    assert web_auth.password_matches(GOOD, login_row(env.db)["password"])


def test_the_first_time_setup_works_only_once(server, env):
    set_password(env.db)
    r = server.post("/api/auth/setup", json={"password": "a takeover attempt"}, headers=HEADER)
    assert r.status_code == 409 and "already set" in r.json()["detail"]
    assert web_auth.password_matches(GOOD, login_row(env.db)["password"])
    assert server.get("/api/summary").status_code == 401


def test_sign_in_and_sign_out(server, env):
    set_password(env.db)
    assert server.get("/api/auth/state").json()["password_set"] is True
    for user, password in (("admin", "wrong password"), ("root", GOOD), ("", GOOD)):
        r = sign_in(server, user, password)
        assert r.status_code == 401 and r.json()["detail"] == "Wrong username or password."
    assert server.get("/api/summary").status_code == 401
    assert sign_in(server, " Admin ", GOOD).status_code == 200                  # the user name is forgiving about case
    assert server.get("/api/summary").status_code == 200 and server.get("/").status_code == 200
    assert server.post("/api/auth/logout", headers=HEADER).status_code == 200
    assert server.get("/api/summary").status_code == 401


def test_resetting_the_password_signs_the_browser_out_straight_away(server, env, cli):
    set_password(env.db)
    sign_in(server)
    assert server.get("/api/summary").status_code == 200
    set_password(env.db, "brand new password")
    assert server.get("/api/summary").status_code == 401
    assert sign_in(server, password=GOOD).status_code == 401 and sign_in(server, password="brand new password").status_code == 200


def test_a_forged_cookie_is_refused(server, env):
    set_password(env.db)
    server.cookies.set(web_auth.COOKIE, f"{int(time.time()) + 999}.{'0' * 64}")
    assert server.get("/api/summary").status_code == 401


def test_many_wrong_passwords_make_sign_in_wait(server, env):
    set_password(env.db)
    for _ in range(10):
        assert sign_in(server, password="guessing wrong").status_code == 401
    r = sign_in(server)                                                         # even the right one waits now
    assert r.status_code == 429 and "Wait ten minutes" in r.json()["detail"]
    server.app.state.wrong_passwords.clear()
    assert sign_in(server).status_code == 200


def test_signing_in_resets_the_count_of_wrong_passwords(server, env):
    set_password(env.db)
    for _ in range(9):
        sign_in(server, password="guessing wrong")
    assert sign_in(server).status_code == 200
    assert server.app.state.wrong_passwords["testclient"] == []


def test_without_sign_in_the_other_tests_reach_the_pages_directly(env):
    import ui_server
    from fastapi.testclient import TestClient
    env.db.parent.mkdir(parents=True, exist_ok=True)
    sp.open_index(env.db).close()
    app = ui_server.create_app(env.db)
    assert TestClient(app, base_url="http://127.0.0.1:8765").get("/api/summary").status_code == 200
    app.state.jobs.shutdown()


# --------------------------------------------------------------------------- the 'password' command
def typed(monkeypatch, *lines):
    monkeypatch.setattr("sys.stdin", io.StringIO("".join(f"{line}\n" for line in lines)))


def test_password_command_sets_it(env, cli, monkeypatch):
    typed(monkeypatch, GOOD, GOOD)
    r = cli("password")
    assert r.code == 0 and "password was changed" in r.err + r.out and GOOD not in r.out + r.err
    assert web_auth.password_matches(GOOD, login_row(env.db)["password"])


def test_password_command_asks_again_after_a_typo_or_a_short_one(env, cli, monkeypatch):
    typed(monkeypatch, GOOD, "correct hrose", "short", GOOD, GOOD)
    r = cli("password")
    assert r.code == 0 and "different" in r.err and "at least 8" in r.err
    assert web_auth.password_matches(GOOD, login_row(env.db)["password"])


def test_password_command_gives_up_after_three_tries(env, cli, monkeypatch):
    set_password(env.db)
    typed(monkeypatch, "aaaaaaaa", "b", "aaaaaaaa", "c", "aaaaaaaa", "d")
    r = cli("password")
    assert r.code == 1 and "Nothing was changed" in r.err
    assert web_auth.password_matches(GOOD, login_row(env.db)["password"])


@pytest.mark.parametrize("lines", [[""], []])
def test_password_command_empty_or_end_of_input_cancels(env, cli, monkeypatch, lines):
    set_password(env.db)
    typed(monkeypatch, *lines)
    r = cli("password")
    assert r.code == 1 and "Cancelled" in r.out + r.err
    assert web_auth.password_matches(GOOD, login_row(env.db)["password"])


def test_password_command_clear_lets_the_web_page_ask_again(env, cli):
    set_password(env.db)
    r = cli("password", "--clear")
    assert r.code == 0 and "asks for a new password" in r.out + r.err and login_row(env.db) is None
    assert cli("password", "--clear").code == 0                                  # nothing to clear is fine too


def test_password_command_without_an_index_makes_one(env, cli, monkeypatch):
    assert not env.db.exists()
    typed(monkeypatch, GOOD, GOOD)
    assert cli("password").code == 0 and env.db.exists()
