import pytest
from fastapi.testclient import TestClient

import main
from config import mask_url
from conftest import TOKEN, URL, tv
from web import create_app

H = {"X-Requested-With": "castmanager"}


@pytest.fixture
def client(make):
    store, backend, wd, clock = make([tv(1), tv(2, url="https://example.com/board/" + TOKEN + "?k=" + TOKEN)])
    wd.check_once()
    c = TestClient(create_app(store, wd, backend), base_url="http://127.0.0.1:8765")
    c.store = store
    return c


def test_mask_url():
    assert mask_url(URL) == "https://dashboard.frazil.app/tv#display=****" + TOKEN[-4:]
    assert TOKEN not in mask_url("https://example.com/x/" + TOKEN + "?t=" + TOKEN)
    assert mask_url("https://example.com/short/page") == "https://example.com/short/page"
    assert mask_url("") == ""


def test_urls_masked_in_page_and_api(client):
    html = client.get("/").text
    api = client.get("/api/tvs").text
    assert "<table>" in html
    for body in (html, api):
        assert TOKEN not in body
        assert TOKEN[:-4] not in body
    assert "****" + TOKEN[-4:] in api


def test_edit_blank_url_keeps_stored_url(client):
    r = client.put("/api/tvs/tv-1", json={"name": "Renamed", "url": ""}, headers=H)
    assert r.status_code == 200
    assert client.store.tv("tv-1")["url"] == URL
    assert client.store.tv("tv-1")["name"] == "Renamed"
    client.put("/api/tvs/tv-1", json={"clear_url": True}, headers=H)
    assert client.store.tv("tv-1")["url"] == ""


def test_add_lookup_discover(client):
    assert client.post("/api/tvs", json={"name": "New", "ip": "10.0.201.1"}, headers=H).status_code == 200
    assert client.post("/api/tvs", json={"name": "Bad", "ip": "nope"}, headers=H).status_code == 400
    assert client.post("/api/lookup", json={"ip": "10.0.201.2"}, headers=H).json()["uuid"] == "uuid-2"
    assert client.post("/api/lookup", json={"ip": "10.9.9.9"}, headers=H).status_code == 502
    assert len(client.get("/api/discover").json()["devices"]) == 2


def test_local_only_guards(client):
    assert client.post("/api/tvs/tv-1/pause").status_code == 403          # no custom header
    assert client.post("/api/tvs/tv-1/pause", headers=H).status_code == 200
    evil = TestClient(client.app, base_url="http://evil.example.com")
    assert evil.get("/api/tvs").status_code == 403


def test_bind_host_is_loopback():
    assert main.HOST == "127.0.0.1"


def test_restart_endpoints(client, monkeypatch):
    import watchdog
    monkeypatch.setattr(watchdog, "RESTART_PAUSE_S", 0)   # the watchdog sleeps for real here
    r = client.post("/api/tvs/tv-1/restart", headers=H)
    assert r.status_code == 200 and r.json()["message"] == "restarted"
    assert client.post("/api/tvs/nope/restart", headers=H).status_code == 409
    assert client.post("/api/restart-all").status_code == 403            # no custom header
    r = client.post("/api/restart-all", headers=H).json()
    assert r["ok"] and r["message"] == "restarted 2 of 2 TVs"
