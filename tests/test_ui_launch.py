"""Starting the web interface from the command line (run_ui), without really starting a server."""
import socket
import sys

import pytest

pytest.importorskip("fastapi")
uvicorn = pytest.importorskip("uvicorn")

import sort_photos as sp  # noqa: E402
import ui_server  # noqa: E402
from conftest import scalar  # noqa: E402


@pytest.fixture
def started(monkeypatch):
    calls = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: calls.update(app=app, **kw))
    return calls


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def run(cli, *extra, port=None, **kw):
    return cli("ui", "--no-browser", "--port", str(port or free_port()), *extra, **kw)


def test_no_index_yet_makes_an_empty_one_so_the_tasks_page_can_sort(env, cli, started):
    r = run(cli)
    assert r.code == 0 and "no index yet" in r.out.lower() and "Tasks page" in r.out and started
    assert scalar(env.db, "SELECT COUNT(*) FROM photos") == 0


def test_starts_on_loopback_by_default(env, cli, started, face_index):
    face_index(5)
    port = free_port()
    r = run(cli, port=port)
    assert r.code == 0 and started["host"] == "127.0.0.1" and started["port"] == port
    assert f"http://127.0.0.1:{port}" in r.out and "stopped" in r.out
    assert "no password" not in r.err


def test_a_port_in_use_is_reported_with_a_suggestion(env, cli, started, face_index):
    face_index(5)
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        r = run(cli, port=port)
    assert r.code == 2 and "Cannot use port" in r.err and f"--port {port + 1}" in r.err and not started


def test_listening_on_the_network_warns_loudly_and_accepts_any_host(env, cli, started, face_index):
    face_index(5)
    r = run(cli, "--host", "0.0.0.0")
    assert "no password" in r.err.lower() and started["host"] == "0.0.0.0"
    from fastapi.testclient import TestClient
    assert TestClient(started["app"], base_url="http://nas.local").get("/api/summary").status_code == 200


def test_loopback_server_refuses_foreign_host_names(env, cli, started, face_index):
    face_index(5)
    run(cli)
    from fastapi.testclient import TestClient
    assert TestClient(started["app"], base_url="http://nas.local").get("/api/summary").status_code == 403


def test_ctrl_c_stops_cleanly(env, cli, monkeypatch, face_index):
    face_index(5)
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt()))
    r = run(cli)
    assert r.code == 0 and "stopped" in r.out


def test_browser_is_opened_unless_told_not_to(env, cli, started, monkeypatch, face_index):
    face_index(5)
    opened = []
    monkeypatch.setattr(ui_server.webbrowser, "open", lambda url: opened.append(url))

    class Instant:
        def __init__(self, delay, fn): self.fn = fn
        def start(self): self.fn()

    monkeypatch.setattr(ui_server.threading, "Timer", Instant)
    cli("ui", "--port", str(free_port()))
    assert len(opened) == 1 and opened[0].startswith("http://127.0.0.1:")
    opened.clear()
    run(cli)
    assert opened == []


def test_a_corrupt_index_is_reported(env, cli, started):
    env.db.parent.mkdir(parents=True)
    env.db.write_bytes(b"definitely not sqlite" * 30)
    r = run(cli)
    assert r.code == 2 and "Cannot open the index" in r.err
