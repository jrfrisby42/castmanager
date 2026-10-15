"""Watchdog: keeps each enabled TV showing its configured url (spec B2 and B3)."""
import logging
import threading
import time
from dataclasses import dataclass

from backend import BACKDROP_APP_ID, DASHCAST_APP_ID
from config import mask_url

log = logging.getLogger("castmanager")

RECAST_MIN_INTERVAL_S = 120   # at most one re-cast per TV every 2 minutes
BACKOFF_START_S = 45          # offline reconnect backoff: 45 s doubling ...
BACKOFF_MAX_S = 300           # ... to a 5 minute cap
QUIT_SETTLE_S = 8             # how long to wait for a TV to go idle after stopping its app


def classify(app_id, display_name):
    """Return 'showing' (DashCast), 'idle' (Backdrop or no app) or 'other'."""
    if app_id == DASHCAST_APP_ID:
        return "showing"
    if not app_id or app_id == BACKDROP_APP_ID or display_name == "Backdrop":
        return "idle"
    return "other"


def wants_recast(kind, takeover):
    """The B3 table: should the watchdog re-cast a TV in this state?"""
    if kind == "showing":
        return False
    if kind == "idle":
        return True
    return takeover == "always"


def cast_refusal(tv, current_tv, device=None):
    """Return why a cast must not happen, or None when it is allowed.

    tv is the TV being cast to, current_tv is the same TV as it is in config right now
    (None if it was removed), device is the connected DeviceStatus (None = skip UUID check).
    """
    if current_tv is None:
        return "not in config"
    if not current_tv.get("enabled"):
        return "disabled"
    if not (current_tv.get("url") or "").strip():
        return "no url"
    if device is not None:
        want = (current_tv.get("uuid") or "").lower()
        if not want:
            return "no UUID in config"
        if device.uuid.lower() != want:
            return "wrong device at this IP"
    return None


@dataclass
class TVState:
    status: str = "starting"
    now_showing: str = ""
    last_check: float | None = None
    last_recast: float | None = None
    recasts_today: int = 0
    recast_day: str = ""
    paused: bool = False
    handle: object = None
    backoff_s: float = 0
    next_attempt: float = 0
    pending: str | None = "startup"   # reason for a cast owed (startup or url change)
    seen: tuple | None = None          # (ip, uuid, url) last applied


class Watchdog:
    def __init__(self, store, backend, clock=time.time, sleep=time.sleep):
        self.store = store
        self.backend = backend
        self.clock = clock
        self.sleep = sleep
        self._states = {}
        self._locks = {}
        self._meta = threading.Lock()

    # ------------------------------------------------------------ helpers

    def _lock_for(self, tv_id):
        with self._meta:
            return self._locks.setdefault(tv_id, threading.RLock())

    def _state(self, tv_id):
        with self._meta:
            return self._states.setdefault(tv_id, TVState())

    def _set_status(self, tv, st, status):
        if st.status != status:
            log.info("%s (%s): status %s -> %s", tv["name"], tv["ip"], st.status, status)
            st.status = status

    def _disconnect(self, st):
        if st.handle is not None:
            try:
                self.backend.disconnect(st.handle)
            except Exception:
                pass
            st.handle = None

    def _track_config(self, tv, st):
        seen = (tv["ip"], tv["uuid"], tv["url"])
        if st.seen is not None:
            if seen[:2] != st.seen[:2]:
                log.info("%s: ip or uuid changed, reconnecting", tv["name"])
                self._disconnect(st)
                st.backoff_s = st.next_attempt = 0
            if seen[2] != st.seen[2] and seen[2]:
                st.pending = "url changed"
        st.seen = seen

    def _ensure_connected(self, tv, st, now):
        if st.handle is not None:
            return st.handle
        if now < st.next_attempt:
            self._set_status(tv, st, "offline")
            return None
        try:
            st.handle = self.backend.connect(tv["ip"], tv["uuid"])
        except Exception as e:
            st.backoff_s = BACKOFF_START_S if not st.backoff_s else min(st.backoff_s * 2, BACKOFF_MAX_S)
            st.next_attempt = now + st.backoff_s
            log.warning("%s (%s): connect failed: %s; retry in %ds", tv["name"], tv["ip"], e, st.backoff_s)
            self._set_status(tv, st, "offline")
            return None
        st.backoff_s = st.next_attempt = 0
        log.info("%s (%s): connected", tv["name"], tv["ip"])
        return st.handle

    def _connected_status(self, tv, st, now):
        """Return (handle, DeviceStatus) or (None, None) when offline. A lost connection gets one
        immediate fresh reconnect; after that the backoff applies."""
        for attempt in (1, 2):
            handle = self._ensure_connected(tv, st, now)
            if handle is None:
                return None, None
            try:
                return handle, self.backend.status(handle)
            except Exception as e:
                log.warning("%s (%s): connection lost: %s", tv["name"], tv["ip"], e)
                self._disconnect(st)
                if attempt == 2:
                    st.backoff_s = BACKOFF_START_S if not st.backoff_s else min(st.backoff_s * 2, BACKOFF_MAX_S)
                    st.next_attempt = now + st.backoff_s
                    self._set_status(tv, st, "offline")
        return None, None

    def _safe_cast(self, tv, st, handle, device, reason, now):
        """The only place that calls backend.cast. Returns (ok, message)."""
        refusal = cast_refusal(tv, self.store.tv(tv["id"]), device)
        if refusal:
            log.warning("%s: refused to cast (%s): %s", tv["name"], reason, refusal)
            return False, refusal
        self._stop_running_app(tv, handle)
        self.backend.cast(handle, tv["url"])
        day = time.strftime("%Y-%m-%d", time.localtime(now))
        if st.recast_day != day:
            st.recast_day, st.recasts_today = day, 0
        st.recasts_today += 1
        st.last_recast = now
        log.info("%s (%s): cast %s, reason: %s", tv["name"], tv["ip"], mask_url(tv["url"]), reason)
        self._set_status(tv, st, "casting")
        return True, "cast sent"

    def _stop_running_app(self, tv, handle):
        """Quit whatever app the TV is running before casting. DashCast force mode replaces its
        own receiver with the page it loads, so a TV already showing a page reports DashCast but
        has nothing left to accept a new url; only a fresh launch takes it (bug seen 2026-10-15)."""
        dev = self.backend.status(handle)
        if classify(dev.app_id, dev.display_name) == "idle":
            return
        log.info("%s: stopping %s before casting", tv["name"], dev.display_name or dev.app_id)
        self.backend.quit(handle)
        waited = 0.0
        while waited < QUIT_SETTLE_S:
            dev = self.backend.status(handle)
            if classify(dev.app_id, dev.display_name) == "idle":
                return
            self.sleep(0.5)
            waited += 0.5
        log.warning("%s: still running %s after stopping it; casting anyway", tv["name"], dev.display_name or dev.app_id)

    # ------------------------------------------------------------ the check

    def check_tv(self, tv_id):
        with self._lock_for(tv_id):
            tv = self.store.tv(tv_id)
            if tv is None:
                return
            st = self._state(tv_id)
            now = self.clock()
            st.last_check = now
            self._track_config(tv, st)

            if not tv["enabled"]:
                self._disconnect(st)
                self._set_status(tv, st, "disabled")
                return
            if st.paused:
                self._set_status(tv, st, "paused")
                return

            handle, dev = self._connected_status(tv, st, now)
            if dev is None:
                return
            st.now_showing = dev.display_name or "nothing"

            refusal = cast_refusal(tv, tv, dev)
            if refusal in ("wrong device at this IP", "no UUID in config"):
                self._set_status(tv, st, "wrong device" if refusal.startswith("wrong") else "no UUID set")
                return

            kind = classify(dev.app_id, dev.display_name)
            takeover = self.store.get()["takeover"]
            if refusal == "no url":
                self._set_status(tv, st, "no url")
                return
            if kind == "showing":
                self._set_status(tv, st, "showing")
            elif kind == "idle":
                self._set_status(tv, st, "idle")
            else:
                self._set_status(tv, st, f"in use by {dev.display_name or dev.app_id}")

            want = wants_recast(kind, takeover)
            if st.pending and (want or kind == "showing"):
                ok, _ = self._safe_cast(tv, st, handle, dev, st.pending, now)
                if ok:
                    st.pending = None
            elif want:
                if st.last_recast is not None and now - st.last_recast < RECAST_MIN_INTERVAL_S:
                    log.info("%s: %s but re-cast held (2 minute limit)", tv["name"], kind)
                else:
                    reason = "TV was idle" if kind == "idle" else f"takeover=always, TV showed {st.now_showing}"
                    self._safe_cast(tv, st, handle, dev, reason, now)

    def check_once(self):
        for tv in self.store.tvs():
            try:
                self.check_tv(tv["id"])
            except Exception:
                # One TV's failure must never stop the loop for the others.
                log.exception("%s (%s): check failed", tv["name"], tv["ip"])
                st = self._state(tv["id"])
                self._disconnect(st)  # start fresh next time
                self._set_status(tv, st, "error")
        with self._meta:
            live = {t["id"] for t in self.store.tvs()}
            for gone in set(self._states) - live:
                self._disconnect(self._states.pop(gone))

    def run(self, stop_event):
        log.info("watchdog started")
        while not stop_event.is_set():
            self.check_once()
            stop_event.wait(self.store.get()["check_interval_s"])
        for st in list(self._states.values()):
            self._disconnect(st)
        log.info("watchdog stopped")

    def check_soon(self, tv_id):
        """Run one check for a TV in the background (after an edit)."""
        def go():
            try:
                self.check_tv(tv_id)
            except Exception:
                log.exception("background check failed for %s", tv_id)
        threading.Thread(target=go, daemon=True).start()

    # ------------------------------------------------------------ actions from the web page

    def cast_now(self, tv_id):
        with self._lock_for(tv_id):
            tv = self.store.tv(tv_id)
            st = self._state(tv_id)
            refusal = cast_refusal(tv, tv)
            if refusal:
                return False, refusal
            st.paused = False
            st.next_attempt = 0  # a manual cast skips the offline backoff
            now = self.clock()
            handle, dev = self._connected_status(tv, st, now)
            if dev is None:
                return False, "offline"
            ok, msg = self._safe_cast(tv, st, handle, dev, "manual cast", now)
            if ok:
                st.pending = None
            return ok, msg

    def stop(self, tv_id):
        with self._lock_for(tv_id):
            tv = self.store.tv(tv_id)
            if tv is None:
                return False, "not in config"
            st = self._state(tv_id)
            st.paused = True
            self._set_status(tv, st, "paused")
            if st.handle is None:
                return True, "paused (not connected, nothing to stop)"
            try:
                dev = self.backend.status(st.handle)
                if cast_refusal(tv, tv, dev) in ("wrong device at this IP", "no UUID in config"):
                    return True, "paused (wrong device, left alone)"
                self.backend.quit(st.handle)
                log.info("%s: stopped casting and paused", tv["name"])
                return True, "stopped and paused"
            except Exception as e:
                log.warning("%s: stop failed: %s", tv["name"], e)
                self._disconnect(st)
                return True, f"paused, but quit failed: {e}"

    def set_paused(self, tv_id, paused):
        with self._lock_for(tv_id):
            tv = self.store.tv(tv_id)
            if tv is None:
                return False, "not in config"
            st = self._state(tv_id)
            st.paused = paused
            log.info("%s: %s", tv["name"], "paused" if paused else "resumed")
            self._set_status(tv, st, "paused" if paused else "resuming")
        if not paused:
            self.check_soon(tv_id)
        return True, "paused" if paused else "resumed"

    def snapshot(self):
        rows = []
        today = time.strftime("%Y-%m-%d", time.localtime(self.clock()))
        for tv in self.store.tvs():
            st = self._state(tv["id"])
            rows.append({
                "id": tv["id"], "name": tv["name"], "ip": tv["ip"], "uuid": tv["uuid"],
                "url_masked": mask_url(tv["url"]), "has_url": bool(tv["url"]),
                "enabled": tv["enabled"], "paused": st.paused, "status": st.status,
                "now_showing": st.now_showing, "last_check": st.last_check,
                "last_recast": st.last_recast,
                "recasts_today": st.recasts_today if st.recast_day == today else 0,
            })
        return rows
