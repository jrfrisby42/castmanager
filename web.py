"""Local web page and JSON API (spec B4). Bound to 127.0.0.1 by main.py."""
import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

log = logging.getLogger("castmanager")
STATIC = Path(__file__).parent / "static"
ALLOWED_HOSTS = {"127.0.0.1", "localhost"}


class TVIn(BaseModel):
    name: str
    ip: str
    uuid: str = ""
    url: str = ""
    enabled: bool = True


class TVEdit(BaseModel):
    name: str | None = None
    ip: str | None = None
    uuid: str | None = None
    url: str | None = None      # blank or missing keeps the stored url
    clear_url: bool = False
    enabled: bool | None = None


class LookupIn(BaseModel):
    ip: str


def create_app(store, watchdog, backend):
    app = FastAPI(title="Cast Manager", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        # Block DNS rebinding (foreign Host header) and cross-site form posts (custom header
        # cannot be sent cross-origin without a CORS preflight, which we never approve).
        host = (request.headers.get("host") or "").rsplit(":", 1)[0]
        if host not in ALLOWED_HOSTS:
            return JSONResponse({"detail": "forbidden host"}, status_code=403)
        if request.method not in ("GET", "HEAD") and request.headers.get("x-requested-with") != "castmanager":
            return JSONResponse({"detail": "missing X-Requested-With header"}, status_code=403)
        return await call_next(request)

    def _bad(e):
        return HTTPException(400, str(e))

    def _result(ok, msg):
        if not ok:
            raise HTTPException(409, msg)
        return {"ok": True, "message": msg}

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})

    @app.get("/api/tvs")
    def list_tvs():
        cfg = store.get()
        return {"takeover": cfg["takeover"], "check_interval_s": cfg["check_interval_s"],
                "tvs": watchdog.snapshot()}

    @app.post("/api/tvs")
    def add_tv(body: TVIn):
        try:
            tv = store.add_tv(body.model_dump())
        except ValueError as e:
            raise _bad(e)
        log.info("added TV %s (%s)", tv["name"], tv["ip"])
        watchdog.check_soon(tv["id"])
        return {"ok": True, "id": tv["id"]}

    @app.put("/api/tvs/{tv_id}")
    def edit_tv(tv_id: str, body: TVEdit):
        data = {k: v for k, v in body.model_dump().items() if v is not None and k != "clear_url"}
        if not data.get("url"):
            data.pop("url", None)
        if body.clear_url:
            data["url"] = ""
        try:
            tv = store.update_tv(tv_id, data)
        except KeyError:
            raise HTTPException(404, "no such TV")
        except ValueError as e:
            raise _bad(e)
        log.info("edited TV %s (%s): %s", tv["name"], tv["ip"], ", ".join(sorted(data)) or "nothing")
        watchdog.check_soon(tv_id)
        return {"ok": True}

    @app.delete("/api/tvs/{tv_id}")
    def delete_tv(tv_id: str):
        try:
            store.delete_tv(tv_id)
        except KeyError:
            raise HTTPException(404, "no such TV")
        log.info("deleted TV %s", tv_id)
        return {"ok": True}

    @app.post("/api/tvs/{tv_id}/cast")
    def cast_now(tv_id: str):
        return _result(*watchdog.cast_now(tv_id))

    @app.post("/api/tvs/{tv_id}/stop")
    def stop(tv_id: str):
        return _result(*watchdog.stop(tv_id))

    @app.post("/api/tvs/{tv_id}/pause")
    def pause(tv_id: str):
        return _result(*watchdog.set_paused(tv_id, True))

    @app.post("/api/tvs/{tv_id}/resume")
    def resume(tv_id: str):
        return _result(*watchdog.set_paused(tv_id, False))

    @app.post("/api/lookup")
    def lookup(body: LookupIn):
        try:
            return backend.lookup(body.ip.strip())
        except Exception as e:
            log.warning("lookup %s failed: %s", body.ip, e)
            raise HTTPException(502, f"lookup failed: {e}")

    @app.get("/api/discover")
    def discover():
        try:
            return {"devices": backend.discover()}
        except Exception as e:
            log.warning("discover failed: %s", e)
            raise HTTPException(502, f"discover failed: {e}")

    return app
