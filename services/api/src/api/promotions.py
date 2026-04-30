"""
api.promotions - filesystem-backed agent-to-model bindings.

Why filesystem and not Redis or Postgres?
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

  Same reasoning as ``api.training``: an operator workflow needs a
  durable, ``cat``-able trail of "which model was deployed when".  A
  flat-file pointer in ``models/active/<agent_id>.json`` plus an
  append-only ``<agent_id>.history.jsonl`` gives us:

    * Zero infra dependency (no DB, no Redis schema migration).
    * Operator-readable state (just open the file).
    * Trivial rollback (pop the last history line).
    * Forward-compatibility: a future Redis cache or pubsub layer
      can treat these files as the source of truth.

Why not Redis?
  Redis is volatile.  A ``FLUSHDB`` or restart-without-AOF would lose
  the "what's deployed" answer.  Flat files survive everything except
  ``rm -rf``.

Why not the strategies table?
  There is no strategies table.  Strategies are emergent properties
  of order routing in the current architecture (see
  ``api.routes.strategies``).  Bindings are agent-level, not strategy-level.

Operational reality
~~~~~~~~~~~~~~~~~~~

  Promotion writes a new pointer; the agent doesn't observe the change
  until it's restarted.  The API never restarts the agent -- that's an
  operator action.  This module's job ends at "the file on disk
  matches the operator's intent".
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import pathlib
import time
from typing import Any

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Configuration                                                              #
# --------------------------------------------------------------------------- #


def _default_models_dir() -> pathlib.Path:
    return pathlib.Path(os.environ.get("MODELS_DIR", "models"))


def _default_active_dir() -> pathlib.Path:
    """The active-model pointer lives next to the model directories so a
    single ``rsync models/`` ships both the trained artifacts and the
    deployment metadata.  Override is mostly for tests."""
    override = os.environ.get("ACTIVE_MODELS_DIR")
    if override:
        return pathlib.Path(override)
    return _default_models_dir() / "active"


# Names that would leak outside MODELS_DIR or break path joins.
_BAD_NAME_CHARS = set("/\\:*?\"<>|\0")


# --------------------------------------------------------------------------- #
# Dataclasses                                                                #
# --------------------------------------------------------------------------- #


@dataclasses.dataclass(frozen=True)
class ActiveBinding:
    """Snapshot of "which model is active for this agent right now".

    All fields except ``agent_id`` and ``model_name`` are optional so
    we can deserialize older / partial records without crashing -- a
    file written by ``cat`` ought to be picked up if it has at least
    those two keys.
    """

    agent_id: str
    model_name: str
    promoted_at: float
    promoted_by: str = "unknown"

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


class PromotionError(ValueError):
    """Raised on validation failures (bad name, missing artifacts, etc)."""


# --------------------------------------------------------------------------- #
# Validation                                                                  #
# --------------------------------------------------------------------------- #


def _validate_agent_id(agent_id: str) -> None:
    """Same anti-traversal rules as model names.

    Agent IDs hit the filesystem (active/<agent_id>.json) so they need
    the same care.  The legitimate values look like
    ``gbm_predictor.v1`` -- letters, digits, dot, dash, underscore.
    """
    if not agent_id or any(ch in _BAD_NAME_CHARS for ch in agent_id):
        raise PromotionError(f"invalid agent_id: {agent_id!r}")
    if agent_id.startswith("."):
        raise PromotionError(f"agent_id cannot start with a dot: {agent_id!r}")


def _validate_model_name(name: str) -> None:
    if not name or any(ch in _BAD_NAME_CHARS for ch in name):
        raise PromotionError(f"invalid model name: {name!r}")
    if name.startswith("."):
        raise PromotionError(f"model name cannot start with a dot: {name!r}")


# --------------------------------------------------------------------------- #
# Store                                                                      #
# --------------------------------------------------------------------------- #


class PromotionStore:
    """Read/write the per-agent active-model pointer + history.

    Shape on disk (under ``active_dir``)::

        gbm_predictor.v1.json          -- current ActiveBinding (single doc)
        gbm_predictor.v1.history.jsonl -- one ActiveBinding per line, append-only

    All filesystem writes are atomic (temp + rename) so a crash mid
    write can't leave the pointer half-flushed.
    """

    def __init__(
        self,
        *,
        models_dir: pathlib.Path | None = None,
        active_dir: pathlib.Path | None = None,
    ) -> None:
        self._models_dir = (
            models_dir if models_dir is not None else _default_models_dir()
        )
        self._active_dir = (
            active_dir if active_dir is not None else _default_active_dir()
        )

    # ------ read paths ----------------------------------------------- #

    def get_active(self, agent_id: str) -> ActiveBinding | None:
        """Return the current binding or ``None`` if no model has been
        promoted yet (the file doesn't exist).

        Malformed JSON is logged and treated as "no binding" rather
        than raised: the operator-facing UI shouldn't 500 because
        someone hand-edited a file."""
        _validate_agent_id(agent_id)
        path = self._active_path(agent_id)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text())
            return ActiveBinding(
                agent_id=data["agent_id"],
                model_name=data["model_name"],
                promoted_at=float(data.get("promoted_at", 0.0)),
                promoted_by=str(data.get("promoted_by", "unknown")),
            )
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
            logger.warning("active binding %s malformed: %s", path.name, exc)
            return None

    def get_history(
        self, agent_id: str, *, limit: int = 50
    ) -> list[ActiveBinding]:
        """Return the most recent ``limit`` promotions, newest first.

        Bad lines are skipped; the file is append-only, so partial
        writes (very rare) only ever truncate the *last* line.
        """
        _validate_agent_id(agent_id)
        path = self._history_path(agent_id)
        if not path.is_file():
            return []
        try:
            lines = path.read_text().splitlines()
        except OSError:
            return []
        out: list[ActiveBinding] = []
        for raw in reversed(lines):
            raw = raw.strip()
            if not raw:
                continue
            try:
                data = json.loads(raw)
                out.append(
                    ActiveBinding(
                        agent_id=data["agent_id"],
                        model_name=data["model_name"],
                        promoted_at=float(data.get("promoted_at", 0.0)),
                        promoted_by=str(data.get("promoted_by", "unknown")),
                    )
                )
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                logger.warning(
                    "history line in %s skipped: %s", path.name, exc
                )
            if len(out) >= limit:
                break
        return out

    # ------ write paths ---------------------------------------------- #

    def promote(
        self,
        *,
        agent_id: str,
        model_name: str,
        promoted_by: str = "operator",
    ) -> ActiveBinding:
        """Validate, write the pointer, append to history."""
        _validate_agent_id(agent_id)
        _validate_model_name(model_name)

        # Refuse to promote a model that doesn't actually exist on
        # disk; the agent would crash on start otherwise and the
        # operator wouldn't know why.
        model_dir = self._models_dir / model_name
        if not model_dir.is_dir():
            raise PromotionError(
                f"model directory not found: {model_dir!s}"
            )
        if not (model_dir / "model.txt").is_file():
            raise PromotionError(
                f"model.txt missing in {model_dir!s} -- can't promote a "
                "model with no booster file"
            )
        if not (model_dir / "meta.json").is_file():
            raise PromotionError(
                f"meta.json missing in {model_dir!s} -- the agent needs it "
                "to know feature names + horizon"
            )

        binding = ActiveBinding(
            agent_id=agent_id,
            model_name=model_name,
            promoted_at=time.time(),
            promoted_by=promoted_by,
        )
        self._active_dir.mkdir(parents=True, exist_ok=True)
        self._atomic_write_json(self._active_path(agent_id), binding.to_dict())
        self._append_history(self._history_path(agent_id), binding)
        return binding

    def rollback(
        self, *, agent_id: str, promoted_by: str = "operator"
    ) -> ActiveBinding | None:
        """Restore the previous binding from history.

        Behaviour:

          * 0 history entries  -> ``None``, no change.
          * 1 history entry    -> ``None``, but the active pointer file is
                                  removed (no model is active).
          * 2+ history entries -> active pointer is rewritten to the
                                  second-most-recent binding, and that
                                  rollback is itself appended to history
                                  so a forward "redo" is also possible.

        The append-on-rollback shape gives us a Git-like timeline: the
        history is the source of truth and every state is reachable.
        """
        _validate_agent_id(agent_id)
        history = self.get_history(agent_id, limit=2)
        active_path = self._active_path(agent_id)
        history_path = self._history_path(agent_id)

        if len(history) == 0:
            return None
        if len(history) == 1:
            # Only one promotion ever; rolling back means clearing.
            if active_path.exists():
                active_path.unlink()
            # Record the clear in history with a sentinel model_name
            # marker so the timeline is honest.
            cleared = ActiveBinding(
                agent_id=agent_id,
                model_name="(rolled-back-to-empty)",
                promoted_at=time.time(),
                promoted_by=promoted_by,
            )
            self._append_history(history_path, cleared)
            return None

        # Two or more entries: target is the second-most-recent.  We
        # re-promote rather than just rewrite the file so the
        # validation rules (model dir present, model.txt exists) are
        # re-applied -- the previous model may have been deleted in
        # the meantime.
        target = history[1]
        return self.promote(
            agent_id=agent_id,
            model_name=target.model_name,
            promoted_by=f"{promoted_by} (rollback)",
        )

    # ------ internals ------------------------------------------------ #

    def _active_path(self, agent_id: str) -> pathlib.Path:
        return self._active_dir / f"{agent_id}.json"

    def _history_path(self, agent_id: str) -> pathlib.Path:
        return self._active_dir / f"{agent_id}.history.jsonl"

    @staticmethod
    def _atomic_write_json(path: pathlib.Path, data: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        os.replace(tmp, path)

    @staticmethod
    def _append_history(path: pathlib.Path, binding: ActiveBinding) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Single-line JSON per the JSONL convention; newline at end.
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(binding.to_dict()))
            f.write("\n")


# --------------------------------------------------------------------------- #
# Module-level singleton                                                     #
# --------------------------------------------------------------------------- #
#
# Same pattern as api.training: a default-constructed singleton for
# production, an injectable test hook for hermetic fixtures.

_store: PromotionStore | None = None


def get_promotion_store() -> PromotionStore:
    global _store
    if _store is None:
        _store = PromotionStore()
    return _store


def reset_promotion_store(
    *,
    models_dir: pathlib.Path | None = None,
    active_dir: pathlib.Path | None = None,
) -> PromotionStore:
    """Test hook: rebuild the singleton against fresh dirs."""
    global _store
    _store = PromotionStore(models_dir=models_dir, active_dir=active_dir)
    return _store


# --------------------------------------------------------------------------- #
# Agent-side helper                                                          #
# --------------------------------------------------------------------------- #
#
# Exported here so the agent process can import a single function
# instead of pulling in the whole api package (which depends on
# fastapi, pydantic etc that an agent shouldn't need).
#
# The agent reads ``models/active/<agent_id>.json`` directly -- we
# duplicate the small amount of parsing logic so the agent never
# imports ``api``.  The contract: ``resolve_active_model_dir`` returns
# the path to load, falling back to the env var and then to the
# trainer-default model name.


def resolve_active_model_dir(
    agent_id: str,
    *,
    models_dir: pathlib.Path | None = None,
    env_fallback: str | None = None,
    default_model: str = "gbm_predictor",
) -> pathlib.Path:
    """Return the ``Path`` an agent should load its model from.

    Resolution order:

      1. ``models/active/<agent_id>.json`` -> ``<models_dir>/<model_name>``
      2. ``$<env_fallback>``  (e.g. ``GBM_MODEL_DIR``)
      3. ``<models_dir>/<default_model>``

    Fail-soft: a corrupted active.json is logged and skipped.  The
    agent would rather fall through to the env-var path than refuse
    to start.
    """
    models = (
        models_dir if models_dir is not None else _default_models_dir()
    )
    active = (
        models / "active" / f"{agent_id}.json"
        if os.environ.get("ACTIVE_MODELS_DIR") is None
        else _default_active_dir() / f"{agent_id}.json"
    )
    if active.is_file():
        try:
            data = json.loads(active.read_text())
            name = data.get("model_name")
            if name:
                return models / name
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
            logger.warning(
                "active binding %s ignored on agent boot: %s",
                active,
                exc,
            )
    if env_fallback and os.environ.get(env_fallback):
        return pathlib.Path(os.environ[env_fallback])
    return models / default_model
