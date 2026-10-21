"""Container HEALTHCHECK for the AutoAir image: probes whatever this container runs.

* ``AUTOAIR_HEALTHCHECK_URL`` set: GET it (2xx = healthy);
* ``autoair serve`` / ``autoair local``: ``/api/health`` on AUTOAIR_SERVER__PORT (8765),
  over HTTPS when AUTOAIR_SERVER__SSL_CERTFILE is set;
* ``autoair ai-serve``: ``/health`` on 8790;
* ``autoair validator-serve``: ``/health`` on its ``--port`` (8791);
* ``airflow api-server``: ``/api/v2/monitor/health`` on 8080;
* ``airflow scheduler``: the scheduler health server on 8974 when enabled;
* anything else (dag-processor, triggerer, one-shot jobs): healthy, use the orchestrator's
  own probe (``airflow jobs check ...``).
"""

from __future__ import annotations

import os
import ssl
import sys
import urllib.request


def _cmdline() -> str:
    try:
        with open("/proc/1/cmdline", "rb") as fh:
            return fh.read().replace(b"\0", b" ").decode(errors="replace")
    except OSError:
        return ""


def _url() -> str | None:
    if os.environ.get("AUTOAIR_HEALTHCHECK_URL"):
        return os.environ["AUTOAIR_HEALTHCHECK_URL"]
    cmd = _cmdline()
    if "ai-serve" in cmd:
        return "http://127.0.0.1:8790/health"
    if "validator-serve" in cmd:
        args = cmd.split()
        port = args[args.index("--port") + 1] if "--port" in args[:-1] else "8791"
        return f"http://127.0.0.1:{port}/health"
    if "autoair serve" in cmd or "autoair local" in cmd:
        scheme = "https" if os.environ.get("AUTOAIR_SERVER__SSL_CERTFILE") else "http"
        port = os.environ.get("AUTOAIR_SERVER__PORT", "8765")
        return f"{scheme}://127.0.0.1:{port}/api/health"
    if "api-server" in cmd:
        return "http://127.0.0.1:8080/api/v2/monitor/health"
    if "scheduler" in cmd and os.environ.get(
        "AIRFLOW__SCHEDULER__ENABLE_HEALTH_CHECK", ""
    ).lower() in ("1", "true"):
        port = os.environ.get("AIRFLOW__SCHEDULER__SCHEDULER_HEALTH_CHECK_SERVER_PORT", "8974")
        return f"http://127.0.0.1:{port}/health"
    return None


def main() -> int:
    url = _url()
    if url is None:
        return 0
    context = ssl.create_default_context()
    context.check_hostname = False  # probing 127.0.0.1 with a certificate for the real name
    context.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(url, timeout=5, context=context) as resp:  # noqa: S310
            return 0 if 200 <= resp.status < 300 else 1
    except Exception as exc:
        print(f"unhealthy: {url}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
