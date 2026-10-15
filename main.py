"""Cast Manager entry point.

    python main.py            real TVs (pychromecast)
    python main.py --fake     fake cast layer, never contacts a TV
"""
import argparse
import logging
import os
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

import uvicorn

from backend import FakeBackend, PyChromecastBackend
from config import ConfigStore
from watchdog import Watchdog
from web import create_app

HOST = "127.0.0.1"  # never bind to other interfaces (spec B4)
APP_DIR = Path(__file__).resolve().parent


def setup_logging(log_dir):
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    logger = logging.getLogger("castmanager")
    logger.setLevel(logging.INFO)
    # 1 MB per file, 5 files in total (the live file plus 4 rotated ones).
    fh = RotatingFileHandler(log_dir / "castmanager.log", maxBytes=1_000_000, backupCount=4, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    if sys.stderr is not None:
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        logger.addHandler(sh)
    return logger


def main(argv=None):
    p = argparse.ArgumentParser(description="Cast Manager")
    p.add_argument("--config", default=str(APP_DIR / "config.json"))
    p.add_argument("--fake", action="store_true", help="use a fake cast layer (no TV is contacted)")
    p.add_argument("--port", type=int, help="override the port in config.json")
    args = p.parse_args(argv)

    # pythonw has no console; give stray prints somewhere harmless to go.
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")

    log = setup_logging(APP_DIR / "logs")
    if not os.path.exists(args.config):
        log.error("config file %s not found; copy config.example.json to config.json", args.config)
        return 2
    store = ConfigStore(args.config)
    cfg = store.get()
    backend = FakeBackend.from_config(cfg) if args.fake else PyChromecastBackend()
    port = args.port or cfg["port"]
    log.info("Cast Manager starting on http://%s:%d (%s backend, takeover=%s)",
             HOST, port, "FAKE" if args.fake else "pychromecast", cfg["takeover"])

    watchdog = Watchdog(store, backend)
    stop = threading.Event()
    thread = threading.Thread(target=watchdog.run, args=(stop,), name="watchdog", daemon=True)
    thread.start()
    try:
        uvicorn.run(create_app(store, watchdog, backend), host=HOST, port=port,
                    log_config=None, access_log=False)
    finally:
        stop.set()
        thread.join(timeout=15)
    return 0


if __name__ == "__main__":
    sys.exit(main())
