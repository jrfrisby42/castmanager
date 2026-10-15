import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import FakeBackend, FakeDevice  # noqa: E402
from config import ConfigStore  # noqa: E402
from watchdog import Watchdog  # noqa: E402

TOKEN = "Zq3vX9kLmN0pQrStUvWxYz_AbCdEfGhIjKlMnOpQ7aB9"  # fake 44-char display token
URL = "https://dashboard.frazil.app/tv#display=" + TOKEN


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


def tv(n, **kw):
    d = {"id": f"tv-{n}", "name": f"TV {n}", "ip": f"10.0.201.{n}", "uuid": f"uuid-{n}",
         "url": URL, "enabled": True}
    d.update(kw)
    return d


@pytest.fixture
def make(tmp_path):
    """make(tvs, takeover) -> (store, backend, watchdog, clock). One idle fake device per TV."""
    def _make(tvs, takeover="idle_only", devices=None):
        p = tmp_path / "config.json"
        p.write_text(json.dumps({"port": 8765, "check_interval_s": 45, "takeover": takeover, "tvs": tvs}))
        store = ConfigStore(str(p))
        if devices is None:
            devices = {t["ip"]: FakeDevice(uuid=t["uuid"] or "x") for t in tvs}
        backend = FakeBackend(devices)
        clock = Clock()
        return store, backend, Watchdog(store, backend, clock=clock), clock
    return _make
