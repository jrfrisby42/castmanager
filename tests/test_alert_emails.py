import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from fastapi.testclient import TestClient

from conftest import tv
from dashboard import DashboardClient, DashboardError
from web import create_app

H = {"X-Requested-With": "castmanager"}
KEY = "test-api-key"


@pytest.fixture
def fake_dashboard():
    """A local stand-in for the dashboard's /admin/alert-emails endpoint."""
    state = {"displays": True, "signins": True, "stale_slides": True, "system_health": True}
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def _reply(self, code, body):
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _handle(self):
            seen.append((self.command, self.headers.get("Authorization")))
            if self.path != "/admin/alert-emails":
                return self._reply(404, {"detail": "Not Found"})
            if self.headers.get("Authorization") != "Bearer " + KEY:
                return self._reply(401, {"detail": "Invalid or missing API key"})
            if self.command == "POST":
                n = int(self.headers.get("Content-Length") or 0)
                state.update(json.loads(self.rfile.read(n)))
            self._reply(200, state)

        do_GET = do_POST = _handle

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", state, seen
    srv.shutdown()


def _client(make, dashboard):
    store, backend, wd, _ = make([tv(1)])
    return TestClient(create_app(store, wd, backend, dashboard), base_url="http://127.0.0.1:8765")


def test_get_and_set_switches(make, fake_dashboard):
    url, state, seen = fake_dashboard
    c = _client(make, DashboardClient(url, KEY))
    r = c.get("/api/alert-emails")
    assert r.json() == {"configured": True, "switches": state}
    r = c.put("/api/alert-emails", json={"displays": False, "signins": False}, headers=H)
    assert r.status_code == 200
    assert r.json()["switches"] == {"displays": False, "signins": False, "stale_slides": True, "system_health": True}
    assert state["displays"] is False and state["stale_slides"] is True
    assert all(auth == "Bearer " + KEY for _, auth in seen)


def test_api_key_never_reaches_the_page(make, fake_dashboard):
    url, _, _ = fake_dashboard
    c = _client(make, DashboardClient(url, KEY))
    for path in ("/", "/api/tvs", "/api/alert-emails"):
        assert KEY not in c.get(path).text


def test_put_needs_custom_header(make, fake_dashboard):
    url, state, _ = fake_dashboard
    c = _client(make, DashboardClient(url, KEY))
    assert c.put("/api/alert-emails", json={"displays": False}).status_code == 403
    assert state["displays"] is True


def test_not_configured(make):
    c = _client(make, DashboardClient("https://dashboard.example", ""))
    assert c.get("/api/alert-emails").json() == {"configured": False}
    assert c.put("/api/alert-emails", json={"displays": False}, headers=H).status_code == 502


def test_bad_key_and_unreachable(make, fake_dashboard):
    url, state, _ = fake_dashboard
    r = _client(make, DashboardClient(url, "wrong")).get("/api/alert-emails")
    assert r.status_code == 502 and "API key" in r.json()["detail"]
    r = _client(make, DashboardClient("http://127.0.0.1:9", KEY, timeout_s=2)).get("/api/alert-emails")
    assert r.status_code == 502 and "could not reach" in r.json()["detail"]
    with pytest.raises(DashboardError, match="not updated"):
        DashboardClient(url + "/old", KEY).alert_emails()


def test_unknown_types_are_not_sent(fake_dashboard):
    url, state, _ = fake_dashboard
    DashboardClient(url, KEY).set_alert_emails({"displays": False, "bogus": False})
    assert "bogus" not in state


def test_config_defaults(make):
    store, _, _, _ = make([tv(1)])
    cfg = store.get()
    assert cfg["dashboard_url"] == "https://dashboard.frazil.app" and cfg["dashboard_api_key"] == ""
