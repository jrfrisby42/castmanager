"""Config file handling and URL masking for Cast Manager.

config.json holds display links, which are secrets, so it is git-ignored.
"""
import copy
import ipaddress
import json
import os
import re
import secrets
import threading
from urllib.parse import urlsplit

DEFAULTS = {"port": 8765, "check_interval_s": 45, "takeover": "idle_only", "tvs": []}
TAKEOVER_MODES = ("idle_only", "always")

# A run of 16+ URL-safe characters looks like a token (display links use 43).
_TOKEN_RE = re.compile(r"[A-Za-z0-9_\-]{16,}")


def _mask_token(token):
    return "****" + token[-4:] if len(token) > 4 else "****"


def mask_url(url):
    """Return url with every token-like part replaced by **** plus its last 4 characters.

    Everything after '#display=' is always treated as a token. Scheme and host stay readable.
    """
    if not url:
        return ""
    head, sep, token = url.partition("#display=")
    if sep:
        return mask_url(head) + sep + _mask_token(token)
    parts = urlsplit(url)
    if parts.scheme and parts.netloc:
        prefix = f"{parts.scheme}://{parts.netloc}"
        rest = url[len(prefix):]
    else:
        prefix, rest = "", url
    return prefix + _TOKEN_RE.sub(lambda m: _mask_token(m.group(0)), rest)


def new_tv_id():
    return "tv-" + secrets.token_hex(3)


def validate_tv(data, partial=False):
    """Validate and normalize TV fields. Raises ValueError with a readable message."""
    out = {}
    if "name" in data or not partial:
        name = str(data.get("name") or "").strip()
        if not name:
            raise ValueError("name is required")
        out["name"] = name
    if "ip" in data or not partial:
        ip = str(data.get("ip") or "").strip()
        try:
            ipaddress.IPv4Address(ip)
        except ValueError:
            raise ValueError(f"not a valid IPv4 address: {ip!r}")
        out["ip"] = ip
    if "uuid" in data or not partial:
        out["uuid"] = str(data.get("uuid") or "").strip().lower()
    if "url" in data or not partial:
        url = str(data.get("url") or "").strip()
        if url and not url.lower().startswith(("http://", "https://")):
            raise ValueError("url must start with http:// or https://")
        out["url"] = url
    if "enabled" in data or not partial:
        out["enabled"] = bool(data.get("enabled", True))
    return out


class ConfigStore:
    """Thread-safe in-memory copy of config.json. Every edit is written back atomically."""

    def __init__(self, path):
        self.path = path
        self._lock = threading.RLock()
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        cfg = {**DEFAULTS, **raw}
        if cfg["takeover"] not in TAKEOVER_MODES:
            raise ValueError(f"takeover must be one of {TAKEOVER_MODES}")
        cfg["port"] = int(cfg["port"])
        cfg["check_interval_s"] = max(5, int(cfg["check_interval_s"]))
        tvs, changed = [], False
        for tv in cfg.get("tvs") or []:
            clean = validate_tv(tv)
            clean["id"] = tv.get("id") or new_tv_id()
            changed = changed or not tv.get("id")
            tvs.append(clean)
        cfg["tvs"] = tvs
        self._cfg = cfg
        if changed:
            self.save()  # persist the generated ids so the web page can address each TV

    def get(self):
        with self._lock:
            return copy.deepcopy(self._cfg)

    def tvs(self):
        with self._lock:
            return copy.deepcopy(self._cfg["tvs"])

    def tv(self, tv_id):
        with self._lock:
            for tv in self._cfg["tvs"]:
                if tv["id"] == tv_id:
                    return dict(tv)
        return None

    def save(self):
        with self._lock:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._cfg, f, indent=2)
            os.replace(tmp, self.path)

    def add_tv(self, data):
        tv = validate_tv(data)
        tv["id"] = new_tv_id()
        with self._lock:
            self._cfg["tvs"].append(tv)
            self.save()
        return dict(tv)

    def update_tv(self, tv_id, data):
        changes = validate_tv(data, partial=True)
        with self._lock:
            for tv in self._cfg["tvs"]:
                if tv["id"] == tv_id:
                    tv.update(changes)
                    self.save()
                    return dict(tv)
        raise KeyError(tv_id)

    def delete_tv(self, tv_id):
        with self._lock:
            before = len(self._cfg["tvs"])
            self._cfg["tvs"] = [t for t in self._cfg["tvs"] if t["id"] != tv_id]
            if len(self._cfg["tvs"]) == before:
                raise KeyError(tv_id)
            self.save()
