"""fincept_core.datasets.settlement - filesystem-backed settlement ledger.

Why this module exists
~~~~~~~~~~~~~~~~~~~~~~

The prediction log (``fincept_core.prediction_log``) records what the
agent *said*; this module records what *happened*.  Each
``SettlementRecord`` joins to a prediction by ``prediction_id`` and
carries the realized (gross + net-of-cost) return, the cost breakdown,
and a Brier score component, plus a status that distinguishes
``pending_time`` (horizon not yet elapsed), ``pending_data`` (horizon
elapsed but prices missing), ``settled``, and ``failed``.

Filesystem layout
~~~~~~~~~~~~~~~~~

  data/settlements/<agent_id>.jsonl

One file per agent, append-only.  Each line is a JSON object with the
``SettlementRecord`` shape below.  Append-only means a re-settlement
under a new cost model appends a *new* row (history preserved) rather
than mutating the old one -- the join key for idempotency is
``(prediction_id, cost_model_version)``.

Design constraints (from the ml-dataset-evidence-spine plan)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

  * **No peeking**: we never store the realized PnL at
    ``decision_window_start_ns``.  The store refuses any append whose
    ``decision_window_end_ns`` is in the future relative to ``now_ns``
    (look-ahead guard).
  * **No auto-rewrite**: settled rows are never mutated in place; a
    new cost-model version appends a new row.
  * **No silent duplicate skip**: a duplicate
    ``(prediction_id, cost_model_version)`` raises
    ``SettlementError(code="duplicate")`` rather than being swallowed.
  * **No services/* import**: semantics mirror
    ``services/quant_foundry/settlement.py`` but the implementation is
    self-contained to avoid a circular dependency.
"""

from __future__ import annotations

import json
import os
import pathlib
import time
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

# --------------------------------------------------------------------------- #
# Configuration                                                              #
# --------------------------------------------------------------------------- #

DEFAULT_COST_MODEL_VERSION = "v1.default"

# v1.default cost model: fee 5 bps, spread 3 bps, slippage 0 bps.
# Exported as a constant so a future audit grep finds the canonical
# values in one place.  Kept as a plain dict (not a dataclass) so the
# settlement module stays dependency-free beyond pydantic.
DEFAULT_COST_MODEL: dict[str, float] = {
    "fee_bps": 5.0,
    "spread_bps": 3.0,
    "slippage_bps": 0.0,
}


def _default_settlements_dir() -> pathlib.Path:
    return pathlib.Path(os.environ.get("SETTLEMENTS_DIR", "data/settlements"))


# Reject agent ids that could escape the settlements dir or break a
# path join.  This is the SAME allow-list as
# Name validation is shared across all stores via fincept_core.naming.
from fincept_core.naming import validate_name as _validate_name


def _validate_agent_id(agent_id: str) -> None:
    _validate_name(agent_id, field="agent_id")


# --------------------------------------------------------------------------- #
# Errors                                                                      #
# --------------------------------------------------------------------------- #


class SettlementError(ValueError):
    """Machine-readable settlement failure.

    ``code`` is a stable string the caller can switch on without
    parsing the message.  Known codes:

      * ``"look_ahead"``           -- ``decision_window_end_ns > now_ns``.
      * ``"duplicate"``            -- ``(prediction_id, cost_model_version)``
                                       already present in the JSONL.
      * ``"invalid_prediction_id"``-- empty ``prediction_id``.
      * ``"missing_settled_at"``   -- ``status="settled"`` but
                                       ``settled_at_ns`` is None.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------------------- #
# Record shape                                                                #
# --------------------------------------------------------------------------- #

SettlementStatus = Literal["pending_time", "pending_data", "settled", "failed"]


class SettlementRecord(BaseModel):
    """One settled (or pending) outcome for a single prediction.

    Frozen (``model_config = ConfigDict(frozen=True, extra="forbid")``)
    so the audit trail is immutable and an accidental extra key is
    rejected at construction time rather than silently dropped.

    The store appends a new record (with a new cost-model version)
    rather than mutating an existing one -- see ``SettlementStore.append``
    for the idempotency rule.

    Join keys:
      * ``prediction_id`` -- joins to ``PredictionRow.id``.
      * ``cost_model_version`` -- disambiguates re-settlements under a
        new cost model; ``(prediction_id, cost_model_version)`` is the
        idempotency key.

    When ``status`` is ``pending_time`` or ``pending_data`` the
    return/metric fields are ``None`` and ``settled_at_ns`` is ``None``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    settlement_schema_version: int = 1
    prediction_id: str
    agent_id: str
    model_name: str
    symbol: str
    ts_event: int
    horizon_ns: int
    decision_window_start_ns: int
    decision_window_end_ns: int
    cost_model_version: str = DEFAULT_COST_MODEL_VERSION
    realized_return_gross: float | None = None
    realized_return_net: float | None = None
    cost_breakdown_fee_bps: float
    cost_breakdown_spread_bps: float
    cost_breakdown_slippage_bps: float = 0.0
    brier_component: float | None = None
    status: SettlementStatus = "pending_time"
    settled_at_ns: int | None = None
    failure_reason: str | None = None

    def to_json(self) -> str:
        """Render to a JSONL line."""
        return self.model_dump_json()

    @field_validator("cost_breakdown_spread_bps")
    @classmethod
    def _spread_bps_sanity_bound(cls, v: float) -> float:
        """Reject absurd spread values (> 100 bps = 1% is a sanity ceiling)."""
        if v > 100.0:
            raise ValueError(
                f"cost_breakdown_spread_bps={v} exceeds sanity bound of 100 bps"
            )
        return v

    @model_validator(mode="after")
    def _decision_window_ordering(self) -> SettlementRecord:
        """decision_window_start_ns must not exceed decision_window_end_ns."""
        if self.decision_window_start_ns > self.decision_window_end_ns:
            raise ValueError(
                "decision_window_start_ns "
                f"({self.decision_window_start_ns}) > "
                f"decision_window_end_ns ({self.decision_window_end_ns})"
            )
        return self

    @classmethod
    def from_json(cls, line: str) -> SettlementRecord:
        """Parse a JSONL line.  Raises on malformed JSON or schema mismatch."""
        return cls.model_validate_json(line)


# --------------------------------------------------------------------------- #
# The store                                                                   #
# --------------------------------------------------------------------------- #


class SettlementStore:
    """Append-only settlement ledger on the filesystem.

    Layout: ``<root>/<agent_id>.jsonl`` (one file per agent,
    append-only, one record per line).  Tests pass a ``tmp_path``;
    production reads from ``$SETTLEMENTS_DIR`` (default
    ``data/settlements``).
    """

    def __init__(self, *, root: pathlib.Path | None = None) -> None:
        self._root = root or _default_settlements_dir()

    @property
    def root(self) -> pathlib.Path:
        return self._root

    def _path(self, agent_id: str) -> pathlib.Path:
        _validate_agent_id(agent_id)
        return self._root / f"{agent_id}.jsonl"

    # ------------------------------------------------------------------ #
    # Write                                                              #
    # ------------------------------------------------------------------ #

    def append(
        self,
        record: SettlementRecord,
        *,
        now_ns: int | None = None,
    ) -> SettlementRecord:
        """Persist a single settlement record and return it.

        Validation order (each raises ``SettlementError`` with a
        distinct ``code``):

          1. ``prediction_id`` non-empty  -> ``invalid_prediction_id``.
          2. ``status="settled"`` requires ``settled_at_ns`` is not None
             -> ``missing_settled_at``.
          3. ``decision_window_end_ns <= now_ns`` (look-ahead guard)
             -> ``look_ahead``.  The boundary
             ``decision_window_end_ns == now_ns`` is allowed.
          4. ``(prediction_id, cost_model_version)`` not already present
             -> ``duplicate``.

        ``now_ns`` defaults to ``time.time_ns()``; tests should pass an
        explicit value for determinism.
        """
        _validate_agent_id(record.agent_id)

        if not record.prediction_id:
            raise SettlementError(
                "invalid_prediction_id",
                "prediction_id must be non-empty",
            )

        if record.status == "settled" and record.settled_at_ns is None:
            raise SettlementError(
                "missing_settled_at",
                "status='settled' requires settled_at_ns to be set",
            )

        wall = now_ns if now_ns is not None else time.time_ns()
        if record.decision_window_end_ns > wall:
            raise SettlementError(
                "look_ahead",
                (
                    "decision_window_end_ns "
                    f"({record.decision_window_end_ns}) > now_ns ({wall}); "
                    "cannot settle before the horizon has elapsed"
                ),
            )

        # Idempotency: a settled OR failed row for
        # (prediction_id, cost_model_version) is terminal -- re-writing
        # with the same cost_model_version raises ``duplicate`` so the
        # caller learns the repeat is a bug, not a no-op.  A non-terminal
        # row (pending_data, pending_time) MAY be superseded by a later
        # append (e.g. a pending_data row followed by a settled row once
        # the price feed catches up); the earlier row is retained as
        # history because the ledger is append-only.  A failed row may
        # be re-settled under a *different* cost_model_version (the
        # ``_find`` scan is keyed on cost_model_version, so a different
        # version simply does not match and is allowed through).
        existing = self._find(record.agent_id, record.prediction_id, record.cost_model_version)
        if existing is not None and existing.status in ("settled", "failed"):
            raise SettlementError(
                "duplicate",
                (
                    f"(prediction_id={record.prediction_id!r}, "
                    f"cost_model_version={record.cost_model_version!r}) "
                    f"already {existing.status} in the settlement ledger"
                ),
            )

        self._root.mkdir(parents=True, exist_ok=True)
        path = self._path(record.agent_id)
        # One write call per line.  On POSIX a write smaller than
        # PIPE_BUF (4096) is atomic; the largest row we expect is
        # ~400 bytes, well under that ceiling.  On NTFS a write that
        # fits in one cluster is atomic for our purposes.
        with path.open("a", encoding="utf-8") as f:
            f.write(record.to_json() + "\n")
        return record

    # ------------------------------------------------------------------ #
    # Read                                                               #
    # ------------------------------------------------------------------ #

    def read(self, prediction_id: str) -> SettlementRecord | None:
        """Return the most recent record for ``prediction_id`` across all agents.

        Scans every agent file since a ``prediction_id`` is globally
        unique (uuid4).  At MVP volumes this is cheap and avoids a
        cross-file index.  Returns ``None`` if no record matches.
        """
        if not self._root.is_dir():
            return None
        latest: SettlementRecord | None = None
        for path in sorted(self._root.glob("*.jsonl")):
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = SettlementRecord.from_json(line)
                    except (json.JSONDecodeError, KeyError, ValueError):
                        # Malformed line must not take the read down --
                        # same tolerance pattern as prediction_log.py:282-286.
                        continue
                    if rec.prediction_id == prediction_id and (
                        latest is None or (rec.settled_at_ns or 0) >= (latest.settled_at_ns or 0)
                    ):
                        # Keep the newest by settled_at_ns (fall back to 0
                        # for pending rows so they sort before settled).
                        latest = rec
        return latest

    def read_for_agent(self, agent_id: str) -> list[SettlementRecord]:
        """Return all records for one agent (oldest-first).

        Returns an empty list if the agent's file does not exist.
        """
        path = self._path(agent_id)
        if not path.is_file():
            return []
        rows: list[SettlementRecord] = []
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(SettlementRecord.from_json(line))
                except (json.JSONDecodeError, KeyError, ValueError):
                    # Malformed line must not take the read down.
                    continue
        return rows

    # ------------------------------------------------------------------ #
    # Internal helpers                                                   #
    # ------------------------------------------------------------------ #

    def _find(
        self,
        agent_id: str,
        prediction_id: str,
        cost_model_version: str,
    ) -> SettlementRecord | None:
        """Return the most recent record for the idempotency key, if any.

        Only scans the single agent file (the join key is scoped to the
        agent's own ledger) -- cheaper than the cross-file ``read``.

        Returns the **last** matching record so that a terminal row
        (settled/failed) that follows a non-terminal row (pending_data/
        pending_time) is correctly detected by the duplicate guard in
        :meth:`append`.  Returning the first match would miss the
        terminal row and allow a duplicate settled row to be appended.
        """
        path = self._path(agent_id)
        if not path.is_file():
            return None
        latest: SettlementRecord | None = None
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = SettlementRecord.from_json(line)
                except (json.JSONDecodeError, KeyError, ValueError):
                    continue
                if (
                    rec.prediction_id == prediction_id
                    and rec.cost_model_version == cost_model_version
                ):
                    latest = rec
        return latest


__all__ = [
    "DEFAULT_COST_MODEL",
    "DEFAULT_COST_MODEL_VERSION",
    "SettlementError",
    "SettlementRecord",
    "SettlementStatus",
    "SettlementStore",
]
