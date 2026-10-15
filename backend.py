"""Cast layer. The watchdog only talks to a CastBackend, so tests (and --fake) swap in FakeBackend
and never touch a real Chromecast."""
import threading
from dataclasses import dataclass, field

DASHCAST_APP_ID = "84912283"
BACKDROP_APP_ID = "E8C28D3C"


@dataclass
class DeviceStatus:
    uuid: str            # the connected device's UUID (lower case)
    app_id: str | None   # running app id, None when nothing is running
    display_name: str | None
    friendly_name: str = ""


class CastBackend:
    """Interface. connect() returns an opaque handle used by the other calls.
    Any method may raise; the watchdog treats failures as a lost connection."""

    def connect(self, ip, uuid):
        raise NotImplementedError

    def status(self, handle):
        raise NotImplementedError

    def cast(self, handle, url):
        raise NotImplementedError

    def quit(self, handle):
        raise NotImplementedError

    def disconnect(self, handle):
        raise NotImplementedError

    def lookup(self, ip):
        """Return {"name", "uuid"} for the device at ip."""
        raise NotImplementedError

    def discover(self):
        """Return a list of {"name", "ip", "uuid", "model"} seen by mDNS."""
        raise NotImplementedError


# ---------------------------------------------------------------- real pychromecast backend

@dataclass
class _Conn:
    cast: object
    dashcast: object = None


class PyChromecastBackend(CastBackend):
    def __init__(self, timeout=10.0):
        self.timeout = timeout

    def _at_ip(self, ip):
        import pychromecast
        casts, browser = pychromecast.get_chromecasts(known_hosts=[ip], timeout=self.timeout)
        return [c for c in casts if str(c.cast_info.host) == ip], browser

    def connect(self, ip, uuid):
        import pychromecast
        at_ip, browser = self._at_ip(ip)
        try:
            if not at_ip:
                raise ConnectionError(f"no Chromecast answered at {ip}")
            want = (uuid or "").lower()
            chosen = next((c for c in at_ip if str(c.cast_info.uuid).lower() == want), at_ip[0])
            # wait() must run BEFORE stop_discovery, or the zeroconf loop dies and wait times out.
            chosen.wait(timeout=self.timeout)
        finally:
            pychromecast.discovery.stop_discovery(browser)
        if not chosen.socket_client.is_connected or chosen.status is None:
            try:
                chosen.disconnect(timeout=2)
            except Exception:
                pass
            raise ConnectionError(f"timed out connecting to {ip}")
        return _Conn(cast=chosen)

    def status(self, handle):
        cast = handle.cast
        if not cast.socket_client.is_connected:
            raise ConnectionError("connection lost")
        return DeviceStatus(
            uuid=str(cast.cast_info.uuid).lower(),
            app_id=cast.app_id,
            display_name=cast.app_display_name,
            friendly_name=cast.cast_info.friendly_name or "",
        )

    def cast(self, handle, url):
        from pychromecast.controllers.dashcast import DashCastController
        if handle.dashcast is None:
            handle.dashcast = DashCastController()
            handle.cast.register_handler(handle.dashcast)
        handle.dashcast.load_url(url, force=True)

    def quit(self, handle):
        handle.cast.quit_app()

    def disconnect(self, handle):
        handle.cast.disconnect(timeout=2)

    def lookup(self, ip):
        import pychromecast
        at_ip, browser = self._at_ip(ip)
        pychromecast.discovery.stop_discovery(browser)
        if not at_ip:
            raise ConnectionError(f"no Chromecast answered at {ip}")
        info = at_ip[0].cast_info
        return {"name": info.friendly_name or "", "uuid": str(info.uuid).lower()}

    def discover(self):
        import pychromecast
        infos, browser = pychromecast.discovery.discover_chromecasts(timeout=5)
        pychromecast.discovery.stop_discovery(browser)
        return sorted(
            ({"name": i.friendly_name or "", "ip": str(i.host), "uuid": str(i.uuid).lower(),
              "model": i.model_name or ""} for i in infos),
            key=lambda d: d["name"].lower(),
        )


# ---------------------------------------------------------------- fake backend (tests and --fake)

@dataclass
class FakeDevice:
    uuid: str
    name: str = "Fake TV"
    app_id: str | None = None
    display_name: str | None = "Backdrop"
    reachable: bool = True
    fail_cast: bool = False


@dataclass
class _FakeHandle:
    ip: str
    alive: bool = True


@dataclass
class FakeBackend(CastBackend):
    devices: dict = field(default_factory=dict)  # ip -> FakeDevice
    calls: list = field(default_factory=list)    # (op, ip, url_or_None)

    def __post_init__(self):
        self._lock = threading.Lock()

    @classmethod
    def from_config(cls, cfg):
        """One idle, reachable fake device per configured TV (used by --fake)."""
        return cls({tv["ip"]: FakeDevice(uuid=tv["uuid"] or "fake-uuid-" + tv["ip"], name=tv["name"])
                    for tv in cfg["tvs"]})

    def casts(self):
        return [c for c in self.calls if c[0] == "cast"]

    def _dev(self, ip):
        dev = self.devices.get(ip)
        if dev is None or not dev.reachable:
            raise ConnectionError(f"fake: {ip} unreachable")
        return dev

    def connect(self, ip, uuid):
        with self._lock:
            self.calls.append(("connect", ip, None))
        self._dev(ip)
        return _FakeHandle(ip)

    def status(self, handle):
        if not handle.alive:
            raise ConnectionError("fake: connection lost")
        dev = self._dev(handle.ip)
        return DeviceStatus(uuid=dev.uuid.lower(), app_id=dev.app_id,
                            display_name=dev.display_name, friendly_name=dev.name)

    def cast(self, handle, url):
        dev = self._dev(handle.ip)
        with self._lock:
            self.calls.append(("cast", handle.ip, url))
        if dev.fail_cast:
            raise RuntimeError(f"fake: cast blew up on {handle.ip}")
        dev.app_id, dev.display_name = DASHCAST_APP_ID, "DashCast"

    def quit(self, handle):
        dev = self._dev(handle.ip)
        with self._lock:
            self.calls.append(("quit", handle.ip, None))
        dev.app_id, dev.display_name = None, "Backdrop"

    def disconnect(self, handle):
        handle.alive = False

    def lookup(self, ip):
        dev = self._dev(ip)
        return {"name": dev.name, "uuid": dev.uuid}

    def discover(self):
        return [{"name": d.name, "ip": ip, "uuid": d.uuid, "model": "Fake"}
                for ip, d in self.devices.items() if d.reachable]
