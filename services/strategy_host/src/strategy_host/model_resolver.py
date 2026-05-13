"""
strategy_host.model_resolver — resolve a ``model_binding`` string to
an on-disk model directory.

The host runs many strategies, each potentially bound to a model
under a stable name like ``"gbm_predictor"`` or ``"gbm_v3"``.  An
operator promotes a candidate by writing a pointer file:

    models/active/<binding>.json  ->  {"model_name": "gbm_2026_04_15"}

This module reads that pointer and returns the resolved path
``models/gbm_2026_04_15``.  The strategy host injects that path into
the strategy's ``params["model_dir"]`` before construction, then
watches the pointer for changes and calls
``strategy.reload_from_dir(new_path)`` if the binding flips.

Why duplicate this from ``api.promotions``?
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

``api.promotions`` already has a near-identical helper.  The
strategy host doesn't depend on ``api`` because:

  * ``api`` ships FastAPI + uvicorn + a stack of HTTP-side
    machinery that has no business in a worker process.
  * Worker processes deploy on different schedules than the API;
    coupling them at the package level forces lock-step deploys
    we don't want.

The agent process (``services/agents/src/agents/gbm_predictor/main.py``)
made the same call: a small duplicated resolver beats a heavy
shared dependency.  This module follows that precedent.

Failure semantics
~~~~~~~~~~~~~~~~~

Unlike the agent's resolver -- which falls through to env-var and
trainer-default to keep the agent running on cold-start -- the host
returns ``None`` on any failure.  The host is more conservative
because it manages N strategies independently and a misnamed
binding silently picking up some other model would be a silent
production incident.  Better to refuse and log loudly.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib

logger = logging.getLogger(__name__)


def _models_root() -> pathlib.Path:
    """Where ``models/<name>/`` artifacts live.

    Defaults to ``./models`` (relative to the host process CWD,
    which is the repo root for ``uv run --package strategy_host``).
    Override via ``MODELS_DIR`` for tests or alternate deployments.
    """
    return pathlib.Path(os.environ.get("MODELS_DIR", "models"))


def _active_dir() -> pathlib.Path:
    """Where the ``<binding>.json`` pointer files live.

    Defaults to ``<models_root>/active``.  Override via
    ``ACTIVE_MODELS_DIR`` for tests that need to write pointers
    into a tmpdir without polluting the repo's models tree.
    """
    override = os.environ.get("ACTIVE_MODELS_DIR")
    if override:
        return pathlib.Path(override)
    return _models_root() / "active"


def pointer_path(binding: str) -> pathlib.Path:
    """Return the absolute path of the pointer file for ``binding``.

    Exposed so the watcher can stat it for mtime changes without
    going through the full resolve path on every poll.
    """
    return _active_dir() / f"{binding}.json"


def resolve_active_model_dir(binding: str) -> pathlib.Path | None:
    """Read the binding's pointer and return the resolved model dir.

    Returns ``None`` if:
      * the pointer file doesn't exist,
      * the file isn't valid JSON,
      * the JSON doesn't have a string ``model_name`` key.

    Each failure mode emits a WARNING log so an operator tailing
    the host logs sees exactly which step broke.  The return path
    is purely lexical -- we don't verify that ``models/<name>``
    actually exists or contains usable artifacts.  That's the
    strategy's job at ``on_start`` (and again at
    ``reload_from_dir``); coupling the resolver to artifact
    inspection would force this module to know about every
    strategy class's expected file layout.
    """
    pointer = pointer_path(binding)
    if not pointer.is_file():
        logger.warning(
            "model_resolver.pointer_missing binding=%s path=%s",
            binding,
            pointer,
        )
        return None
    try:
        data = json.loads(pointer.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(
            "model_resolver.pointer_unreadable binding=%s err=%r",
            binding,
            exc,
        )
        return None
    name = data.get("model_name") if isinstance(data, dict) else None
    if not isinstance(name, str) or not name:
        logger.warning(
            "model_resolver.pointer_invalid binding=%s data=%r",
            binding,
            data,
        )
        return None
    return _models_root() / name
