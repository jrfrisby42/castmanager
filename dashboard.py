"""Alert-email switches on the dashboard server (GET/POST /admin/alert-emails).

The dashboard emails the system alert channel when a TV display goes offline, an external
dashboard's sign-in needs renewing, a TV slide stops updating, or system health changes. Each
kind has an on/off switch there; this reads and sets them with the dashboard's API key.
"""
import json
import urllib.error
import urllib.request

ALERT_EMAIL_TYPES = ("displays", "signins", "stale_slides", "system_health")


class DashboardError(Exception):
    pass


class DashboardClient:
    def __init__(self, base_url, api_key, timeout_s=10):
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.timeout_s = timeout_s

    def configured(self):
        return bool(self.base_url and self.api_key)

    def _request(self, method, path, body=None):
        if not self.configured():
            raise DashboardError("dashboard_url and dashboard_api_key are not set in config.json")
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base_url + path, data=data, method=method)
        req.add_header("Authorization", "Bearer " + self.api_key)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as r:
                return json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise DashboardError("the dashboard rejected the API key (dashboard_api_key in config.json)")
            if e.code == 404:
                raise DashboardError("the dashboard has no alert-email switches (server not updated?)")
            raise DashboardError(f"dashboard returned HTTP {e.code}")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise DashboardError(f"could not reach the dashboard: {getattr(e, 'reason', e)}")

    def alert_emails(self):
        return self._request("GET", "/admin/alert-emails")

    def set_alert_emails(self, changes):
        return self._request("POST", "/admin/alert-emails",
                             {k: bool(v) for k, v in changes.items() if k in ALERT_EMAIL_TYPES})
