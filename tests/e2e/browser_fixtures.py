"""Fixtures for the browser tests: start the real web interface in a subprocess and drive it with Playwright (Chromium)."""
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

sync_api = pytest.importorskip("playwright.sync_api", reason="Playwright is not installed (./run-tests.sh --install --all)")
pytest.importorskip("fastapi")
pytest.importorskip("uvicorn")

import sort_photos as sp  # noqa: E402
import web_auth  # noqa: E402
from conftest import cluster_args  # noqa: E402

PASSWORD = "e2e password"


@pytest.fixture(scope="session")
def browser():
    with sync_api.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as error:      # no browser downloaded
            pytest.skip(f"Chromium is not available ({str(error).splitlines()[0]}); run: python -m playwright install chromium")
        yield b
        b.close()


def start_ui(env, password=PASSWORD):
    """Start 'sort_photos.py ui' on a free port. With a password, site.sign_in(context) signs a browser in directly."""
    token = None
    if password:
        conn = sp.open_index(env.db)
        web_auth.set_password(conn, password)
        conn.close()
        token = web_auth.make_token(web_auth.load(env.db))
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = subprocess.Popen([sys.executable, str(ROOT / "sort_photos.py"), "ui", "--db", str(env.db), "--port", str(port), "--no-browser"],
                            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            urllib.request.urlopen(url + "/api/auth/state", timeout=1).read()
            break
        except Exception:
            if proc.poll() is not None:
                pytest.fail("the web interface did not start:\n" + proc.stdout.read())
            time.sleep(0.2)
    else:
        proc.kill()
        pytest.fail("the web interface did not start in time")
    site = type("Site", (), {"url": url, "library": env.dest, "env": env, "token": token,
                             "cookie": f"{web_auth.COOKIE}={token}",
                             "sign_in": staticmethod(lambda context: context.add_cookies([{"name": web_auth.COOKIE, "value": token, "url": url}]))})()
    return site, proc


def stop_ui(proc):
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture
def site(env, face_index):
    """A running server over a synthetic library: people Ann/Bob/Cy are NOT named yet. A password is set."""
    import face_cluster
    index = face_index(8, 6, 4, strangers=2)
    assert face_cluster.cluster_faces(cluster_args(db=env.db, apply=True, min_group_size=3)) == 0
    index.close()
    site, proc = start_ui(env)
    yield site
    stop_ui(proc)


@pytest.fixture
def new_site(env, face_index):
    """A running server whose password has not been chosen yet."""
    face_index(3).close()
    site, proc = start_ui(env, password=None)
    yield site
    stop_ui(proc)


@pytest.fixture
def page(browser, site):
    context = browser.new_context(viewport={"width": 1360, "height": 900})
    site.sign_in(context)
    page = context.new_page()
    page.js_errors = []
    page.on("pageerror", lambda e: page.js_errors.append(str(e)))
    yield page
    errors = page.js_errors
    context.close()
    assert errors == [], f"JavaScript errors in the page: {errors}"
