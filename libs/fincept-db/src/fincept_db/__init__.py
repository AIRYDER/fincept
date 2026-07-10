import os

# SQLAlchemy's C extension (.pyd) can trigger a MemoryError on Windows
# during platform.machine() → subprocess.Popen in the cyextension loader.
# Disable it before any sqlalchemy import to prevent the crash.
# The pure-Python path works identically for our workload.
if os.name == "nt" and "SQLALCHEMY_DISABLE_CYEXT" not in os.environ:
    os.environ["SQLALCHEMY_DISABLE_CYEXT"] = "1"

from . import (
    audit,
    bars,
    callback_tables,
    engine,
    features,
    models,
    observability,
    provider_data,
    registry_tables,
    ticks,
    universe,
)

__all__ = [
    "audit",
    "bars",
    "callback_tables",
    "engine",
    "features",
    "models",
    "observability",
    "provider_data",
    "registry_tables",
    "ticks",
    "universe",
]
