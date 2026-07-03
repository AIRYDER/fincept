# ruff: noqa: T201 - stdout prints are the container's log channel
"""Startup security preflight for the trainer-gpu-tree worker.

Run before the handler starts. Exits non-zero (fail closed) if a forbidden
env var is present, so a misconfigured image can never reach the training loop.

Can be skipped with QF_DIAG_SKIP_PREFLIGHT=1 for diagnostic builds.
"""
from __future__ import annotations

import ipaddress
import os
import socket
import sys
from urllib.parse import urlparse

# Env vars the worker must NEVER carry. Presence of any of these means the
# image was misconfigured with trading/broker/storage credentials, which
# violates the security boundary (pure function over inputs).
FORBIDDEN_ENV = [
    "REDIS_URL",
    "REDIS_HOST",
    "FINCEPT_JWT_SECRET",
    "ALPACA_API_KEY",
    "ALPACA_SECRET_KEY",
    "ALPACA_API_SECRET",
    "DATABASE_URL",
    "DB_URL",
    "POSTGRES_URL",
    "KAFKA_BOOTSTRAP_SERVERS",
    "BROKER_URL",
    "AMQP_URL",
    "MONGO_URL",
    "MONGODB_URI",
]

# Callback URL is optional (the worker returns the callback in its response,
# it does not POST). If set, the host must not be loopback/private in
# production mode.
_CALLBACK_URL_ENV = "QUANT_FOUNDRY_CALLBACK_URL"
_MODE_ENV = "QUANT_FOUNDRY_TRAINING_MODE"


def _redact(value: str) -> str:
    """Redact a secret-like value, keeping only the first and last char."""
    if not value:
        return "<empty>"
    if len(value) <= 4:
        return "***"
    return f"{value[0]}***{value[-1]}"


def _host_is_private(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        ip = info[4][0]
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if addr.is_loopback or addr.is_private or addr.is_link_local or addr.is_reserved:
            return True
    return False


def main() -> int:
    mode = os.environ.get(_MODE_ENV, "canary").lower()
    print(f"[preflight] training_mode={mode}", flush=True)

    # 1. Forbidden env vars — fail closed regardless of mode.
    violations = [k for k in FORBIDDEN_ENV if os.environ.get(k)]
    if violations:
        print(
            f"[preflight] FAIL: forbidden env vars present: {sorted(violations)}",
            file=sys.stderr,
            flush=True,
        )
        return 2

    # 2. Validate callback URL host (if provided).
    cb_url = os.environ.get(_CALLBACK_URL_ENV, "")
    if cb_url:
        parsed = urlparse(cb_url)
        host = parsed.hostname or ""
        if host and _host_is_private(host) and mode == "production":
            print(
                f"[preflight] FAIL: callback URL host {host!r} is loopback/private "
                f"in production mode",
                file=sys.stderr,
                flush=True,
            )
            return 3
        print(f"[preflight] callback_url host={host} (ok)", flush=True)
    else:
        print("[preflight] callback_url not set (worker returns callback in response)", flush=True)

    # 3. Redacted config summary.
    secret = os.environ.get("QUANT_FOUNDRY_CALLBACK_SECRET", "")
    print("[preflight] redacted config summary:", flush=True)
    print(f"  QUANT_FOUNDRY_CALLBACK_SECRET={_redact(secret)}", flush=True)
    print(f"  QUANT_FOUNDRY_USE_REAL_TRAINER={os.environ.get('QUANT_FOUNDRY_USE_REAL_TRAINER', 'false')}", flush=True)
    print(f"  QUANT_FOUNDRY_TRAINING_DEADLINE_SECONDS={os.environ.get('QUANT_FOUNDRY_TRAINING_DEADLINE_SECONDS', '600')}", flush=True)
    print(f"  QUANT_FOUNDRY_GIT_SHA={os.environ.get('QUANT_FOUNDRY_GIT_SHA', 'unknown')}", flush=True)
    print("[preflight] OK", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
