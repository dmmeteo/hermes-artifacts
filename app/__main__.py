"""Run the service: ``python -m app`` (env: ARTIFACTS_HOST, ARTIFACTS_PORT, see config.py)."""

import logging
import os

import uvicorn

from .config import Settings
from .main import create_app

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
uvicorn.run(
    create_app(Settings.from_env()),
    host=os.environ.get("ARTIFACTS_HOST", "127.0.0.1"),
    port=int(os.environ.get("ARTIFACTS_PORT", "8080")),
    access_log=False,
    server_header=False,
    proxy_headers=True,
    forwarded_allow_ips=os.environ.get("ARTIFACTS_TRUSTED_PROXIES", "127.0.0.1"),
    limit_concurrency=int(os.environ.get("ARTIFACTS_MAX_CONNECTIONS", "64")),
    timeout_keep_alive=5,
    h11_max_incomplete_event_size=16 * 1024,
)
