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

from conftest import cluster_args  # noqa: E402


@pytest.fixture(scope="session")
def browser():
    with sync_api.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as error:      # no browser downloaded
            pytest.skip(f"Chromium is not available ({str(error).splitlines()[0]}); run: python -m playwright install chromium")
        yield b
        b.close()


@pytest.fixture
def site(env, face_index):
    """A running server over a synthetic library: people Ann/Bob/Cy are NOT named yet."""
    import face_cluster
    index = face_index(8, 6, 4, strangers=2)
    assert face_cluster.cluster_faces(cluster_args(db=env.db, apply=True, min_group_size=3)) == 0
    index.close()
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = subprocess.Popen([sys.executable, str(ROOT / "sort_photos.py"), "ui", "--db", str(env.db), "--port", str(port), "--no-browser"],
                            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            urllib.request.urlopen(url + "/api/summary", timeout=1).read()
            break
        except Exception:
            if proc.poll() is not None:
                pytest.fail("the web interface did not start:\n" + proc.stdout.read())
            time.sleep(0.2)
    else:
        proc.kill()
        pytest.fail("the web interface did not start in time")
    site = type("Site", (), {"url": url, "library": env.dest, "env": env})()
    yield site
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture
def page(browser, site):
    context = browser.new_context(viewport={"width": 1360, "height": 900})
    page = context.new_page()
    page.js_errors = []
    page.on("pageerror", lambda e: page.js_errors.append(str(e)))
    yield page
    errors = page.js_errors
    context.close()
    assert errors == [], f"JavaScript errors in the page: {errors}"
