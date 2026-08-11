"""Single-process production topology for process-owned background workers."""

from __future__ import annotations

import os

bind = os.environ.get("GCHAT_BIND", "127.0.0.1:8080")
workers = 1
worker_class = "gthread"
threads = int(os.environ.get("GCHAT_HTTP_THREADS", "8"))
preload_app = True
timeout = 120
graceful_timeout = 120
keepalive = 5
accesslog = "-"
errorlog = "-"
capture_output = True


def post_fork(_server: object, _worker: object) -> None:
    # Threads and flock descriptors must be created in the sole worker, never
    # in Gunicorn's preloaded master process.
    from app import start_runtime_services

    if not start_runtime_services():
        raise RuntimeError("runtime service startup failed")


def worker_exit(_server: object, _worker: object) -> None:
    from app import stop_runtime_services

    stop_runtime_services()
