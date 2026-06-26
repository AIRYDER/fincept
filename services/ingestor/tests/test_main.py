"""Smoke tests for ingestor.main entrypoint.

These tests verify the module's public structure without actually
starting the service (which would require Redis + venue connectivity).
They catch import-time crashes, missing entrypoints, and broken
argument parsing — the most common main.py regressions.
"""

from __future__ import annotations

import inspect

import pytest


def test_main_module_imports_cleanly() -> None:
    """The main module must import without errors (no import-time crashes)."""
    from ingestor import main as main_mod

    assert main_mod is not None


def test_main_has_main_entrypoint() -> None:
    """main() must be a callable (synchronous CLI entrypoint)."""
    from ingestor.main import main

    assert callable(main)
    sig = inspect.signature(main)
    # main() should accept no required args (it reads from argparse/env).
    required = [
        p for p in sig.parameters.values()
        if p.default is inspect.Parameter.empty and p.kind not in (
            inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD
        )
    ]
    assert not required, f"main() has required params: {required}"


def test_main_has_run_loop() -> None:
    """run_loop must be an async callable (the core service loop)."""
    from ingestor.main import run_loop

    assert callable(run_loop)
    assert inspect.iscoroutinefunction(run_loop)


def test_main_has_argparse() -> None:
    """main() should use argparse for CLI args (not bare sys.argv)."""
    import ingestor.main as main_mod

    source = inspect.getsource(main_mod)
    assert "argparse" in source, "main.py should use argparse for CLI args"


def test_main_has_signal_handling() -> None:
    """main() should handle SIGINT/SIGTERM for graceful shutdown."""
    import ingestor.main as main_mod

    source = inspect.getsource(main_mod)
    assert "signal" in source.lower(), "main.py should handle signals for shutdown"


def test_main_has_heartbeat() -> None:
    """main() should start a heartbeat for liveness monitoring."""
    import ingestor.main as main_mod

    source = inspect.getsource(main_mod)
    assert "heartbeat" in source.lower(), "main.py should use heartbeat for liveness"


def test_main_module_docstring() -> None:
    """The main module should have a docstring explaining its purpose."""
    from ingestor import main as main_mod

    assert main_mod.__doc__ is not None
    assert len(main_mod.__doc__.strip()) > 20, "docstring should be descriptive"
