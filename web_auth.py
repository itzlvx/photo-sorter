"""Sign-in for the web interface: one user, admin, with a password kept (hashed) in the index.

    python sort_photos.py password          set a new password (asks twice; signs everyone out)
    python sort_photos.py password --clear  remove it: the web page then asks for a new one

The first time the web page is opened without a password, it asks you to choose one. The
password is stored as a salted PBKDF2-SHA256 hash, never in plain text. A signed-in browser
keeps a cookie for 30 days; it is signed with a secret that changes with every new password,
so setting or clearing the password signs every browser out. Standard library only.
"""
from __future__ import annotations

import getpass
import hashlib
import hmac
import secrets
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

USERNAME = "admin"
MIN_LENGTH = 8
MAX_LENGTH = 200
ITERATIONS = 240_000
COOKIE = "photo_sorter_session"
SESSION_DAYS = 30

TABLE = """CREATE TABLE IF NOT EXISTS web_login (
    username   TEXT PRIMARY KEY,
    password   TEXT NOT NULL,   -- pbkdf2_sha256$iterations$salt$hash
    secret     TEXT NOT NULL,   -- signs the session cookies; new with every password
    changed_at TEXT NOT NULL
)"""


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(TABLE)


def hash_password(password: str, salt: str | None = None, iterations: int = ITERATIONS) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), iterations).hex()
    return f"pbkdf2_sha256${iterations}${salt}${digest}"


def password_matches(password: str, stored: str) -> bool:
    try:
        scheme, iterations, salt, _ = stored.split("$")
        if scheme != "pbkdf2_sha256":
            return False
        return hmac.compare_digest(hash_password(password, salt, int(iterations)), stored)
    except ValueError:
        return False


def problem_with(password: str) -> str | None:
    """Why a new password is not acceptable, or None."""
    if len(password) < MIN_LENGTH:
        return f"The password needs at least {MIN_LENGTH} characters."
    if len(password) > MAX_LENGTH:
        return f"The password can have at most {MAX_LENGTH} characters."
    if password.strip() != password:
        return "The password can't start or end with a space."
    return None


def load(db_path: Path) -> sqlite3.Row | None:
    """The admin login, or None when no password has been set (or there is no index)."""
    if not Path(db_path).exists():
        return None
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM web_login WHERE username = ?", (USERNAME,)).fetchone()
    except sqlite3.OperationalError:          # no table yet: no password yet
        return None
    finally:
        conn.close()


def set_password(conn: sqlite3.Connection, password: str) -> None:
    ensure_table(conn)
    conn.execute(
        "INSERT INTO web_login (username, password, secret, changed_at) VALUES (?, ?, ?, ?)"
        " ON CONFLICT(username) DO UPDATE SET password = excluded.password, secret = excluded.secret, changed_at = excluded.changed_at",
        (USERNAME, hash_password(password), secrets.token_hex(32), datetime.now().isoformat(timespec="seconds")),
    )
    conn.commit()


def set_first_password(conn: sqlite3.Connection, password: str) -> bool:
    """Set the password only if none is set yet (the web page's first-time setup). False if one already was."""
    ensure_table(conn)
    added = conn.execute(
        "INSERT OR IGNORE INTO web_login (username, password, secret, changed_at) VALUES (?, ?, ?, ?)",
        (USERNAME, hash_password(password), secrets.token_hex(32), datetime.now().isoformat(timespec="seconds")),
    ).rowcount
    conn.commit()
    return added == 1


def clear_password(conn: sqlite3.Connection) -> bool:
    ensure_table(conn)
    gone = conn.execute("DELETE FROM web_login WHERE username = ?", (USERNAME,)).rowcount
    conn.commit()
    return gone > 0


def _signature(login: sqlite3.Row, expires: int) -> str:
    return hmac.new(bytes.fromhex(login["secret"]), f"{login['username']}|{expires}".encode(), hashlib.sha256).hexdigest()


def make_token(login: sqlite3.Row, days: int = SESSION_DAYS, now: float | None = None) -> str:
    expires = int((now or time.time()) + days * 86400)
    return f"{expires}.{_signature(login, expires)}"


def token_ok(login: sqlite3.Row | None, token: str | None, now: float | None = None) -> bool:
    if login is None or not token:
        return False
    expires, _, signature = token.partition(".")
    if not expires.isdigit() or int(expires) < (now or time.time()):
        return False
    return hmac.compare_digest(_signature(login, int(expires)), signature)


# --------------------------------------------------------------------------- the 'password' command
def _ask(prompt: str) -> str | None:
    if sys.stdin.isatty():
        try:
            return getpass.getpass(prompt)
        except EOFError:
            return None
    print(prompt, end="", flush=True)             # piped (tests, scripts): read a plain line
    line = sys.stdin.readline()
    print()
    return None if line == "" else line.rstrip("\r\n")


def password_command(args) -> int:
    """0 = changed, 1 = nothing changed (cancelled or not accepted), 2 = the index could not be opened."""
    import sort_photos as sp

    log = sp.log
    db_path = (args.db or sp.default_db_path()).resolve()
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sp.open_index(db_path)
    except (OSError, sqlite3.Error) as error:
        log.error("Cannot open the index %s: %s", db_path, error)
        return 2
    try:
        if args.clear:
            if clear_password(conn):
                log.info("The password was removed and every browser is signed out. "
                         "The web page asks for a new password the next time it is opened.")
            else:
                log.info("No password was set, so nothing changed.")
            return 0
        print(f"New password for the web interface user '{USERNAME}' (at least {MIN_LENGTH} characters; "
              "nothing is shown while you type; leave it empty to cancel).")
        for _ in range(3):
            first = _ask("New password: ")
            if not first:
                log.info("Cancelled. Nothing was changed.")
                return 1
            problem = problem_with(first)
            if problem:
                log.warning(problem)
                continue
            if _ask("Type it again: ") != first:
                log.warning("The two passwords are different. Try again.")
                continue
            set_password(conn, first)
            log.info("The password was changed. Every browser is signed out; sign in again as '%s'.", USERNAME)
            return 0
        log.error("No password was set. Nothing was changed.")
        return 1
    finally:
        conn.close()
