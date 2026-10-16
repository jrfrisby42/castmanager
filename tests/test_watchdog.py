import pytest

from backend import DASHCAST_APP_ID, FakeDevice
from conftest import URL, tv
from watchdog import classify, wants_recast


def settle(wd, backend, clock):
    """Run the startup cast, then forget it, so later checks are pure watchdog decisions."""
    wd.check_once()
    clock.advance(600)
    backend.calls.clear()


def connect_times(backend):
    return len([c for c in backend.calls if c[0] == "connect"])


# ---------------------------------------------------------------- 1. the B3 table

@pytest.mark.parametrize("takeover", ["idle_only", "always"])
@pytest.mark.parametrize("app_id,display,status,recast", [
    (DASHCAST_APP_ID, "DashCast", "showing", {"idle_only": False, "always": False}),
    ("E8C28D3C", "Backdrop", "idle", {"idle_only": True, "always": True}),
    (None, None, "idle", {"idle_only": True, "always": True}),
    ("233637DE", "YouTube", "in use by YouTube", {"idle_only": False, "always": True}),
    ("0F5096E8", "Chrome Mirroring", "in use by Chrome Mirroring", {"idle_only": False, "always": True}),
])
def test_b3_table(make, takeover, app_id, display, status, recast):
    store, backend, wd, clock = make([tv(1)], takeover=takeover)
    settle(wd, backend, clock)
    dev = backend.devices["10.0.201.1"]
    dev.app_id, dev.display_name = app_id, display
    wd.check_once()
    assert (len(backend.casts()) == 1) == recast[takeover]
    expected = "casting" if recast[takeover] else status
    assert wd.snapshot()[0]["status"] == expected


@pytest.mark.parametrize("takeover", ["idle_only", "always"])
def test_b3_offline_backoff(make, takeover):
    store, backend, wd, clock = make([tv(1)], takeover=takeover)
    backend.devices["10.0.201.1"].reachable = False
    attempts = []
    for _ in range(40):
        before = connect_times(backend)
        wd.check_once()
        if connect_times(backend) > before:
            attempts.append(clock.t)
        assert wd.snapshot()[0]["status"] == "offline"
        clock.advance(45)
    gaps = [b - a for a, b in zip(attempts, attempts[1:])]
    # 45 s doubling, then capped at 300 s (seen as 315 s because checks run on a 45 s tick)
    assert gaps[:3] == [45, 90, 180]
    assert set(gaps[3:]) == {315}
    assert wd._states["tv-1"].backoff_s == 300
    assert not backend.casts()


@pytest.mark.parametrize("takeover", ["idle_only", "always"])
def test_b3_lost_connection_reconnects_fresh(make, takeover):
    store, backend, wd, clock = make([tv(1)], takeover=takeover)
    settle(wd, backend, clock)
    wd._states["tv-1"].handle.alive = False  # connection dropped
    wd.check_once()
    assert ("connect", "10.0.201.1", None) in backend.calls
    assert wd.snapshot()[0]["status"] == "showing"


@pytest.mark.parametrize("takeover", ["idle_only", "always"])
def test_b3_wrong_device(make, takeover):
    store, backend, wd, clock = make([tv(1)], takeover=takeover,
                                     devices={"10.0.201.1": FakeDevice(uuid="someone-else")})
    for _ in range(3):
        wd.check_once()
        clock.advance(600)
    assert wd.snapshot()[0]["status"] == "wrong device"
    assert backend.casts() == []


def test_pure_table_functions():
    assert classify(DASHCAST_APP_ID, "DashCast") == "showing"
    assert classify(None, None) == "idle"
    assert classify("E8C28D3C", "Backdrop") == "idle"
    assert classify("233637DE", "YouTube") == "other"
    assert [wants_recast(k, "idle_only") for k in ("showing", "idle", "other")] == [False, True, False]
    assert [wants_recast(k, "always") for k in ("showing", "idle", "other")] == [False, True, True]


# ---------------------------------------------------------------- 2. UUID mismatch never casts

def test_uuid_mismatch_never_casts(make):
    store, backend, wd, clock = make([tv(1), tv(2, uuid="")], takeover="always",
                                     devices={"10.0.201.1": FakeDevice(uuid="someone-else"),
                                              "10.0.201.2": FakeDevice(uuid="whatever")})
    for _ in range(5):
        wd.check_once()
        clock.advance(600)
    assert wd.cast_now("tv-1") == (False, "wrong device at this IP")
    assert wd.cast_now("tv-2") == (False, "no UUID in config")
    assert backend.casts() == []
    assert [r["status"] for r in wd.snapshot()] == ["wrong device", "no UUID set"]


# ---------------------------------------------------------------- 3. disabled / empty url never casts

def test_disabled_or_empty_url_never_casts(make):
    store, backend, wd, clock = make([tv(1, enabled=False), tv(2, url=""), tv(3, url="   ")],
                                     takeover="always")
    for _ in range(5):
        wd.check_once()
        clock.advance(600)
    for tv_id in ("tv-1", "tv-2", "tv-3"):
        ok, msg = wd.cast_now(tv_id)
        assert not ok and msg in ("disabled", "no url")
    assert backend.casts() == []
    assert [r["status"] for r in wd.snapshot()] == ["disabled", "no url", "no url"]


def test_tv_removed_from_config_never_casts(make):
    store, backend, wd, clock = make([tv(1)])
    store.delete_tv("tv-1")
    assert wd.cast_now("tv-1") == (False, "not in config")
    assert backend.casts() == []


# ---------------------------------------------------------------- 4. 2 minute re-cast limit

@pytest.mark.parametrize("takeover", ["idle_only", "always"])
def test_two_idle_detections_60s_apart_one_recast(make, takeover):
    store, backend, wd, clock = make([tv(1)], takeover=takeover)
    settle(wd, backend, clock)
    dev = backend.devices["10.0.201.1"]
    dev.app_id, dev.display_name = None, "Backdrop"
    wd.check_once()                                   # idle -> re-cast
    dev.app_id, dev.display_name = None, "Backdrop"   # TV fell back to idle again
    clock.advance(60)
    wd.check_once()                                   # idle again 60 s later -> held
    assert len(backend.casts()) == 1
    clock.advance(61)                                 # 121 s after the first re-cast
    wd.check_once()
    assert len(backend.casts()) == 2


# ---------------------------------------------------------------- 5. per-TV exception isolation

def test_exception_for_one_tv_does_not_stop_others(make):
    store, backend, wd, clock = make([tv(1), tv(2), tv(3)])
    backend.devices["10.0.201.1"].fail_cast = True   # backend.cast raises for TV 1
    wd.check_once()
    assert [c[1] for c in backend.casts()] == ["10.0.201.1", "10.0.201.2", "10.0.201.3"]
    assert [r["status"] for r in wd.snapshot()] == ["error", "casting", "casting"]


def test_unexpected_error_in_check_isolated(make, monkeypatch):
    store, backend, wd, clock = make([tv(1), tv(2)])
    real = wd._connected_status

    def broken(tv_, st, now):
        if tv_["id"] == "tv-1":
            raise KeyError("unexpected")
        return real(tv_, st, now)
    monkeypatch.setattr(wd, "_connected_status", broken)
    wd.check_once()
    assert [c[1] for c in backend.casts()] == ["10.0.201.2"]


# ---------------------------------------------------------------- startup, url change, stop/pause

def test_cast_once_at_startup_and_on_url_change(make):
    store, backend, wd, clock = make([tv(1)])
    backend.devices["10.0.201.1"].app_id = DASHCAST_APP_ID  # already on DashCast
    wd.check_once()
    assert len(backend.casts()) == 1                       # startup cast
    clock.advance(45)
    wd.check_once()
    assert len(backend.casts()) == 1                       # showing -> nothing
    store.update_tv("tv-1", {"url": URL + "x"})
    clock.advance(10)                                      # inside 2 min, url change still casts
    wd.check_once()
    assert backend.casts()[-1] == ("cast", "10.0.201.1", URL + "x")
    clock.advance(45)
    wd.check_once()
    assert len(backend.casts()) == 2


def test_startup_cast_respects_idle_only_for_other_apps(make):
    store, backend, wd, clock = make([tv(1)])
    dev = backend.devices["10.0.201.1"]
    dev.app_id, dev.display_name = "233637DE", "YouTube"
    wd.check_once()
    assert backend.casts() == []
    dev.app_id, dev.display_name = None, "Backdrop"
    clock.advance(45)
    wd.check_once()
    assert len(backend.casts()) == 1


def test_stop_quits_and_pauses(make):
    store, backend, wd, clock = make([tv(1)])
    wd.check_once()
    backend.calls.clear()
    assert wd.stop("tv-1")[0]
    assert ("quit", "10.0.201.1", None) in backend.calls
    clock.advance(600)
    wd.check_once()
    assert backend.casts() == [] and wd.snapshot()[0]["status"] == "paused"
    wd._states["tv-1"].paused = False   # resume without the background thread
    wd.check_once()
    assert len(backend.casts()) == 1


# ---------------------------------------------------------------- stop the running app before casting

def _ops(backend, ip="10.0.201.1"):
    return [op for op, i, _ in backend.calls if i == ip and op in ("quit", "cast")]


def test_url_change_on_dashcast_tv_quits_then_casts(make):
    # DashCast force mode leaves no receiver behind, so a new url only takes after a fresh launch.
    store, backend, wd, clock = make([tv(1)])
    backend.devices["10.0.201.1"].app_id = DASHCAST_APP_ID
    wd.check_once()                                        # startup cast onto a DashCast TV
    assert _ops(backend) == ["quit", "cast"]
    store.update_tv("tv-1", {"url": URL + "x"})
    clock.advance(10)
    wd.check_once()
    assert _ops(backend) == ["quit", "cast", "quit", "cast"]
    assert backend.casts()[-1] == ("cast", "10.0.201.1", URL + "x")


def test_idle_tv_casts_without_quit(make):
    store, backend, wd, clock = make([tv(1)])
    wd.check_once()
    assert _ops(backend) == ["cast"]


def test_takeover_always_quits_other_app_first(make):
    store, backend, wd, clock = make([tv(1)], takeover="always")
    dev = backend.devices["10.0.201.1"]
    dev.app_id, dev.display_name = "233637DE", "YouTube"
    wd.check_once()
    assert _ops(backend) == ["quit", "cast"]


def test_app_that_will_not_stop_still_gets_cast_after_settle(make):
    store, backend, wd, clock = make([tv(1)])
    dev = backend.devices["10.0.201.1"]
    dev.app_id = DASHCAST_APP_ID
    slept = []
    wd.sleep = slept.append
    backend.quit = lambda handle: backend.calls.append(("quit", handle.ip, None))  # app ignores quit
    wd.check_once()
    assert _ops(backend) == ["quit", "cast"]
    assert sum(slept) == 8                                 # waited QUIT_SETTLE_S, never really slept


# ---------------------------------------------------------------- scheduled refresh

def _showing(backend, ip="10.0.201.1"):
    dev = backend.devices[ip]
    dev.app_id, dev.display_name = DASHCAST_APP_ID, "DashCast"


def test_showing_tv_is_recast_after_refresh_interval(make):
    store, backend, wd, clock = make([tv(1)])
    wd.check_once()                      # startup cast at t0
    _showing(backend)
    clock.advance(3 * 3600 - 60)
    backend.calls.clear()
    wd.check_once()
    assert backend.casts() == []         # not yet
    clock.advance(60)
    wd.check_once()
    assert len(backend.casts()) == 1
    assert ("quit",) in [c[:1] for c in backend.calls]   # relaunch, not a reload of the old app
    _showing(backend)
    clock.advance(45)
    backend.calls.clear()
    wd.check_once()
    assert backend.casts() == []         # the clock restarted at the refresh


def test_refresh_interval_zero_turns_it_off(make):
    store, backend, wd, clock = make([tv(1)])
    store._cfg["refresh_interval_s"] = 0
    wd.check_once()
    _showing(backend)
    clock.advance(24 * 3600)
    backend.calls.clear()
    wd.check_once()
    assert backend.casts() == []


def test_refresh_skips_paused_and_other_apps(make):
    store, backend, wd, clock = make([tv(1), tv(2)])
    wd.check_once()
    _showing(backend, "10.0.201.1")
    dev2 = backend.devices["10.0.201.2"]
    dev2.app_id, dev2.display_name = "233637DE", "YouTube"
    wd.set_paused("tv-1", True)
    clock.advance(4 * 3600)
    backend.calls.clear()
    wd.check_once()
    assert backend.casts() == []


def test_refresh_interval_has_a_floor(tmp_path):
    import json
    from config import ConfigStore
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"refresh_interval_s": 30, "tvs": []}))
    assert ConfigStore(str(p)).get()["refresh_interval_s"] == 600
    p.write_text(json.dumps({"tvs": []}))
    assert ConfigStore(str(p)).get()["refresh_interval_s"] == 10800
