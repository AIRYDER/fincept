"""
fincept_core.strategy_config - filesystem-backed strategy instance configs.

Why this module exists
~~~~~~~~~~~~~~~~~~~~~~

A strategy is a *recipe* (a Strategy class) and an *instance* (the
recipe applied to specific symbols, params, and an optional bound
model).  Phase F adds a Strategy Host service that runs instances
continuously, but the host has no truth source for "what should be
running right now" -- it needs persistent operator-set configs that
survive restarts of either the host or the api.

``StrategyConfigStore`` is that source of truth.  It mirrors the
``api.promotions`` model: filesystem-backed, atomic writes, append-
only history JSONL, ``cat``-able state, survives ``FLUSHDB``.

Filesystem layout
~~~~~~~~~~~~~~~~~

  strategies/<strategy_id>.json                -- current StrategyConfig
  strategies/<strategy_id>.history.jsonl       -- one StrategyConfig per
                                                  line, newest at end,
                                                  append-only audit trail

Why filesystem and not Redis?
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

  Same reasoning as ``api.promotions``: a config defines an
  operator-visible binding ("strategy X uses model Y"), it changes
  rarely, and ``cat strategies/btc_ma.json`` is the right debug
  workflow.  Redis is volatile; ``FLUSHDB`` would lose the entire
  set of running strategies and the operator wouldn't know what to
  rebuild.

Validation
~~~~~~~~~~

The store validates only the filesystem-safety of ``strategy_id``;
``class_name`` and ``params`` shape are validated by callers (api
routes when they accept POST bodies, the host when it instantiates
the class).  This keeps the store dumb and persistence-only and
avoids a fincept-core -> services/backtester import cycle.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import pathlib
import time
from typing import Any

from fincept_core.naming import BAD_NAME_CHARS as _BAD_NAME_CHARS

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Configuration                                                              #
# --------------------------------------------------------------------------- #


def _default_configs_dir() -> pathlib.Path:
    """Where strategy config files live.

    Default is ``strategies/`` next to ``models/`` so a single
    ``rsync ./`` ships configs alongside the models they bind to.
    Override via ``$STRATEGIES_DIR`` for tests / containers.
    """
    return pathlib.Path(os.environ.get("STRATEGIES_DIR", "strategies"))


class StrategyConfigError(ValueError):
    """Raised on validation failures (bad id, malformed disk state)."""


def _validate_strategy_id(strategy_id: str) -> None:
    """Reject ids that would escape the configs dir or break a join."""
    if not strategy_id or any(c in _BAD_NAME_CHARS for c in strategy_id):
        raise StrategyConfigError(f"invalid strategy_id: {strategy_id!r}")
    if strategy_id in {".", ".."} or strategy_id.startswith("."):
        raise StrategyConfigError(f"strategy_id may not start with '.': {strategy_id!r}")


# --------------------------------------------------------------------------- #
# Dataclass                                                                   #
# --------------------------------------------------------------------------- #


@dataclasses.dataclass(frozen=True)
class StrategyConfig:
    """One persistent strategy instance config.

    ``strategy_id`` is the operator-visible identifier (e.g.
    ``btc_ma_crossover_main``); it is also the strategy_id stamped on
    every OrderIntent the host emits, so the portfolio service can
    attribute fills back to it via the OMS audit log.

    ``class_name`` is the registry key (one of ``"buy_and_hold"``,
    ``"ma_crossover"``, ``"gbm"`` -- mirrors
    ``backtester.strategies.STRATEGY_REGISTRY``).  The store does not
    validate this against the runtime registry; callers do.  This
    keeps fincept-core independent of services/backtester.

    ``params`` is a free-form dict of constructor kwargs.  ``Decimal``
    values arrive as strings on the wire; the host coerces them at
    instantiation time (mirrors ``backtester.runner.build_strategy``).
    A free-form dict avoids a typed schema per class so adding a new
    strategy class doesn't require a fincept-core release.

    ``model_binding`` is the agent_id whose active model this strategy
    uses; only meaningful for ``class_name == "gbm"``.  When the
    agent's active pointer changes (``api.promotions.promote``), the
    host's hot-reload watcher swaps the strategy's booster.

    ``enabled`` is the run flag the host respects.  The host watches
    the configs dir periodically and starts/stops runners to match;
    flipping ``enabled`` is the start/stop API.
    """

    strategy_id: str
    class_name: str
    symbols: list[str]
    params: dict[str, Any]
    model_binding: str | None
    enabled: bool
    created_at: float
    updated_at: float

    def to_dict(self) -> dict[str, Any]:
        """Render to a stable dict for JSON serialisation.

        Lists and dicts are copied so a downstream mutation can't
        leak back into the frozen record's storage (frozen prevents
        rebinding, not list-append).
        """
        return {
            "strategy_id": self.strategy_id,
            "class_name": self.class_name,
            "symbols": list(self.symbols),
            "params": dict(self.params),
            "model_binding": self.model_binding,
            "enabled": bool(self.enabled),
            "created_at": float(self.created_at),
            "updated_at": float(self.updated_at),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StrategyConfig:
        """Tolerant constructor: missing optional keys default sanely.

        Required keys (``strategy_id``, ``class_name``) raise KeyError
        if absent so a corrupt file fails loud rather than silently
        instantiating an empty record.  Optional keys default so an
        older on-disk version without ``model_binding`` (added in
        Phase F) still loads.
        """
        return cls(
            strategy_id=str(data["strategy_id"]),
            class_name=str(data["class_name"]),
            symbols=list(data.get("symbols") or []),
            params=dict(data.get("params") or {}),
            model_binding=(
                None if data.get("model_binding") in (None, "") else str(data["model_binding"])
            ),
            enabled=bool(data.get("enabled", False)),
            created_at=float(data.get("created_at", 0.0)),
            updated_at=float(data.get("updated_at", 0.0)),
        )


# --------------------------------------------------------------------------- #
# Store                                                                      #
# --------------------------------------------------------------------------- #


class StrategyConfigStore:
    """Read/write strategy configs + audit history on the filesystem.

    The store is persistence-only: it does not interpret class_name,
    params, or model_binding semantically.  The api validates those
    at the request layer; the host validates them again at
    instantiation time.

    All filesystem writes are atomic (temp + rename) so a crash mid-
    write can't leave a half-flushed config on disk.
    """

    def __init__(
        self,
        *,
        configs_dir: pathlib.Path | None = None,
    ) -> None:
        self._configs_dir = configs_dir if configs_dir is not None else _default_configs_dir()

    @property
    def configs_dir(self) -> pathlib.Path:
        return self._configs_dir

    # ------ paths ----------------------------------------------------- #

    def _config_path(self, strategy_id: str) -> pathlib.Path:
        return self._configs_dir / f"{strategy_id}.json"

    def _history_path(self, strategy_id: str) -> pathlib.Path:
        return self._configs_dir / f"{strategy_id}.history.jsonl"

    # ------ read paths ----------------------------------------------- #

    def get(self, strategy_id: str) -> StrategyConfig | None:
        """Return the current config or ``None`` if not set.

        Malformed JSON is logged and treated as "no config" rather
        than raised, matching the tolerance pattern in
        ``api.promotions.get_active``.
        """
        _validate_strategy_id(strategy_id)
        path = self._config_path(strategy_id)
        if not path.is_file():
            return None
        try:
            return StrategyConfig.from_dict(json.loads(path.read_text()))
        except (
            OSError,
            json.JSONDecodeError,
            KeyError,
            TypeError,
            ValueError,
        ) as exc:
            logger.warning("strategy config %s malformed: %s", path.name, exc)
            return None

    def list_all(self) -> list[StrategyConfig]:
        """All configs sorted by strategy_id, newest config-state.

        Skips ``*.history.jsonl`` and any junk file in the dir.
        Sorting is alphabetical so the dashboard shows a stable order.
        """
        if not self._configs_dir.is_dir():
            return []
        out: list[StrategyConfig] = []
        for path in sorted(self._configs_dir.glob("*.json")):
            stem = path.stem  # filename without ".json"
            # ``foo.history.jsonl`` doesn't match *.json, but a
            # hypothetical ``foo.history.json`` would; explicit guard
            # prevents the parse from confusingly half-succeeding.
            if stem.endswith(".history"):
                continue
            try:
                _validate_strategy_id(stem)
            except StrategyConfigError:
                logger.warning(
                    "strategy config file %s has unsafe stem; skipped",
                    path.name,
                )
                continue
            cfg = self.get(stem)
            if cfg is not None:
                out.append(cfg)
        return out

    def get_history(self, strategy_id: str, *, limit: int = 50) -> list[StrategyConfig]:
        """Most-recent ``limit`` config snapshots, newest first.

        The history file is append-only; on partial-write only the
        last line is at risk (which is the most-recent record).  We
        skip malformed lines rather than failing the whole read --
        the operator-facing UI shouldn't 500 on a corrupt audit.
        """
        _validate_strategy_id(strategy_id)
        if limit < 1:
            raise StrategyConfigError("limit must be >= 1")
        path = self._history_path(strategy_id)
        if not path.is_file():
            return []
        try:
            lines = path.read_text().splitlines()
        except OSError:
            return []
        out: list[StrategyConfig] = []
        for raw in reversed(lines):
            raw = raw.strip()
            if not raw:
                continue
            try:
                out.append(StrategyConfig.from_dict(json.loads(raw)))
            except (
                json.JSONDecodeError,
                KeyError,
                TypeError,
                ValueError,
            ) as exc:
                logger.warning("history line in %s skipped: %s", path.name, exc)
            if len(out) >= limit:
                break
        return out

    # ------ write paths ---------------------------------------------- #

    def upsert(self, config: StrategyConfig) -> StrategyConfig:
        """Atomically write the config + append to history.

        Timestamp policy:
          * ``created_at`` is preserved from the existing record on
            disk if present; otherwise set to wall-clock now.
          * ``updated_at`` is always set to wall-clock now -- the
            caller's value is ignored.  This guarantees a strict
            monotonic order so the host's config watcher can use
            ``updated_at`` as a change-detection trigger.

        Returns the sealed config that was actually written, with
        timestamps applied.  Callers should use this return value
        rather than the input they passed.
        """
        _validate_strategy_id(config.strategy_id)
        now = time.time()
        existing = self.get(config.strategy_id)
        created_at = existing.created_at if existing else now
        sealed = StrategyConfig(
            strategy_id=config.strategy_id,
            class_name=config.class_name,
            symbols=list(config.symbols),
            params=dict(config.params),
            model_binding=config.model_binding,
            enabled=bool(config.enabled),
            created_at=created_at,
            updated_at=now,
        )
        self._configs_dir.mkdir(parents=True, exist_ok=True)
        self._atomic_write_json(self._config_path(sealed.strategy_id), sealed.to_dict())
        self._append_history(self._history_path(sealed.strategy_id), sealed)
        return sealed

    def set_enabled(self, strategy_id: str, *, enabled: bool) -> StrategyConfig:
        """Flip the run flag.  Raises if the config doesn't exist.

        Idempotent: if the flag already matches, no rewrite happens
        and no history line is appended.  This avoids audit-log spam
        from a UI that pessimistically POSTs the current state.
        """
        _validate_strategy_id(strategy_id)
        existing = self.get(strategy_id)
        if existing is None:
            raise StrategyConfigError(
                f"strategy {strategy_id!r} not found; create it before toggling enabled"
            )
        if existing.enabled == enabled:
            return existing
        return self.upsert(dataclasses.replace(existing, enabled=enabled))

    def delete(self, strategy_id: str) -> bool:
        """Remove the current config file.  History is retained.

        Idempotent: returns ``True`` if a file was removed, ``False``
        if the config didn't exist.  We deliberately keep the
        history file so a future audit can answer "what was strategy
        X bound to before someone deleted it?".

        Appends a tombstone record (class_name="(deleted)",
        enabled=False) to history on successful delete so the
        timeline shows the removal as a distinct event rather than
        just trailing off.
        """
        _validate_strategy_id(strategy_id)
        path = self._config_path(strategy_id)
        if not path.is_file():
            return False
        # Read the current state before we unlink so the tombstone
        # carries the same field values the operator was looking at
        # when they decided to delete.
        existing = self.get(strategy_id)
        path.unlink()
        if existing is not None:
            tomb = dataclasses.replace(
                existing,
                class_name="(deleted)",
                enabled=False,
                updated_at=time.time(),
            )
            self._append_history(self._history_path(strategy_id), tomb)
        return True

    # ------ internals ------------------------------------------------ #

    @staticmethod
    def _atomic_write_json(path: pathlib.Path, data: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        os.replace(tmp, path)

    @staticmethod
    def _append_history(path: pathlib.Path, config: StrategyConfig) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Single-line JSON per the JSONL convention; newline at end so
        # the next append starts cleanly.
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(config.to_dict()))
            f.write("\n")


# --------------------------------------------------------------------------- #
# Module-level singleton                                                     #
# --------------------------------------------------------------------------- #
#
# Same pattern as api.promotions / api.training: a default-constructed
# singleton for production, an injectable test hook for hermetic
# fixtures.

_store: StrategyConfigStore | None = None


def get_strategy_config_store() -> StrategyConfigStore:
    """Return the process-wide store, lazy-initialised."""
    global _store
    if _store is None:
        _store = StrategyConfigStore()
    return _store


def reset_strategy_config_store(*, configs_dir: pathlib.Path | None = None) -> StrategyConfigStore:
    """Test hook: rebuild the singleton against a fresh dir."""
    global _store
    _store = StrategyConfigStore(configs_dir=configs_dir)
    return _store
