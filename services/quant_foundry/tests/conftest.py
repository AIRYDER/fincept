"""Shared fixtures for quant_foundry tests.

When running the full suite across all 6 packages on Windows, event
loops created by ``pytest-asyncio`` in the API tests can leak into
the quant_foundry test session and trigger
``ResourceWarning: unclosed event loop`` during garbage collection.
The ``filterwarnings = ["error"]`` setting in ``pyproject.toml``
turns that warning into a test failure.

The autouse fixture below closes any non-closed event loop after each
test, preventing the warning at its source.  A fallback warning filter
catches any loop that GCs before the fixture runs.
"""

from __future__ import annotations

import asyncio
import warnings

import pytest


@pytest.fixture(autouse=True)
def _close_lingering_event_loops() -> None:
    """Close any non-closed event loop after each test."""
    yield
    try:
        # asyncio.get_event_loop() is deprecated when no loop is running,
        # but it is the only way to access a loop that was created by
        # pytest-asyncio and not yet closed.  We suppress the
        # DeprecationWarning from the call itself.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            loop = asyncio.get_event_loop()
        if not loop.is_closed():
            loop.close()
    except RuntimeError:
        # No current event loop â€” nothing to close.
        pass
    except Exception:
        # Swallow any unexpected cleanup error so it never fails a test.
        pass
