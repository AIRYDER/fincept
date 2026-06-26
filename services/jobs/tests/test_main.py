"""Smoke tests for jobs.main entrypoint.

Verifies module structure without starting the service.
"""

from __future__ import annotations

import inspect

import pytest


def test_main_module_imports_cleanly() -> None:
    from jobs import main as main_mod

    assert main_mod is not None


def test_main_has_main_entrypoint() -> None:
    from jobs.main import main

    assert callable(main)
    sig = inspect.signature(main)
    required = [
        p for p in sig.parameters.values()
        if p.default is inspect.Parameter.empty and p.kind not in (
            inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD
        )
    ]
    assert not required, f"main() has required params: {required}"


def test_main_module_docstring() -> None:
    from jobs import main as main_mod

    assert main_mod.__doc__ is not None
    assert len(main_mod.__doc__.strip()) > 20
