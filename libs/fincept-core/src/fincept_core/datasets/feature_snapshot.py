"""fincept_core.datasets.feature_snapshot - filesystem-backed feature snapshots.

Why this module exists
~~~~~~~~~~~~~~~~~~~~~~

The prediction log (``fincept_core.prediction_log``) records what the
agent *said*; the settlement ledger (``fincept_core.datasets.settlement``)
records what *happened*.  This module records what the agent *saw* at
decision time: a frozen :class:`FeatureSnapshot` of the feature rows
that fed a prediction.  Without this, an operator who sees a bad
prediction cannot answer "was the input garbage, or did the model
misfire?" -- the snapshot is the evidence spine's third leg.

Filesystem layout
~~~~~~~~~~~~~~~~~

  data/feature_snapshots/<agent_id>.jsonl

One file per agent, append-only.  Each line is a JSON object of the
shape ``{"prediction_id": "...", "snapshot": <FeatureSnapshot dict>}``
-- the ``prediction_id`` is a sidecar field (``FeatureSnapshot`` itself
has no such field, by design) so that
:meth:`FeatureSnapshotStore.append_if_missing` can de-duplicate without
a separate index.  The snapshot payload is serialised via
``FeatureSnapshot.model_dump()`` (Pydantic v2) and deserialised via
``FeatureSnapshot.model_validate(json.loads(...))``.

Design constraints (from the ml-dataset-evidence-spine plan, todo 4)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

  * **No PIT enforcement here**: the orchestrator / agent is responsible
    for passing ``decision_time_ns <= now_ns``.  The store does not
    re-check; the ``FeatureSnapshot`` model itself already rejects rows
    whose ``ts`` exceeds ``decision_time_ns`` (look-ahead guard lives
    on the schema, not the store).
  * **No registry**: the orchestrator already knows which snapshot it
    produced; the store is a dumb append-only log.
  * **No services/* import**: semantics mirror
    ``services/quant_foundry/feature_snapshot_export.py`` but the
    implementation is self-contained to avoid a circular dependency.
  * **Symmetric agent-id validation**: the ``_validate_agent_id``
    allow-list is copied verbatim from
    ``fincept_core.prediction_log._validate_agent_id`` (lines 72-78) so
    a future audit grep finds the identical forbidden-character set in
    every store.
"""

from __future__ import annotations

import json
import os
import pathlib
from typing import Any

from fincept_core.datasets.schemas import FeatureSnapshot
from fincept_core.naming import validate_name as _validate_name

# --------------------------------------------------------------------------- #
# Configuration                                                               #
# --------------------------------------------------------------------------- #


def _default_snapshots_dir() -> pathlib.Path:
    return pathlib.Path(os.environ.get("FEATURE_SNAPSHOTS_DIR", "data/feature_snapshots"))


def _validate_agent_id(agent_id: str) -> None:
    _validate_name(agent_id, field="agent_id")


# --------------------------------------------------------------------------- #
# JSONL line shape                                                            #
# --------------------------------------------------------------------------- #
#
# Each line is ``{"prediction_id": str, "snapshot": <FeatureSnapshot dict>}``.
# The ``prediction_id`` sidecar lets ``append_if_missing`` de-duplicate
# without a separate index file.  We do NOT embed ``prediction_id`` into
# ``FeatureSnapshot`` itself (the schema is intentionally prediction-
# agnostic so it can be reused for batch backtests).


def _encode_line(prediction_id: str, snapshot: FeatureSnapshot) -> str:
    """Render a (prediction_id, snapshot) pair to a JSONL line."""
    payload: dict[str, Any] = {
        "prediction_id": prediction_id,
        "snapshot": snapshot.model_dump(),
    }
    return json.dumps(payload, separators=(",", ":"))


def _decode_line(line: str) -> tuple[str, FeatureSnapshot] | None:
    """Parse a JSONL line.

    Returns ``(prediction_id, snapshot)`` or ``None`` if the line is
    malformed (bad JSON, missing keys, or schema-validation failure).
    Skipping malformed lines matches the tolerance pattern in
    ``prediction_log.py:282-286``.
    """
    try:
        obj = json.loads(line)
        prediction_id = obj["prediction_id"]
        snapshot = FeatureSnapshot.model_validate(obj["snapshot"])
    except (json.JSONDecodeError, KeyError, ValueError, TypeError):
        return None
    if not isinstance(prediction_id, str):
        return None
    return prediction_id, snapshot


# --------------------------------------------------------------------------- #
# The store                                                                   #
# --------------------------------------------------------------------------- #


class FeatureSnapshotStore:
    """Append-only feature-snapshot log on the filesystem.

    Layout: ``<root>/<agent_id>.jsonl`` (one file per agent,
    append-only, one record per line).  Tests pass a ``root``;
    production reads from ``$FEATURE_SNAPSHOTS_DIR`` (default
    ``data/feature_snapshots``).

    The store does NOT enforce point-in-time correctness -- the
    orchestrator / agent is responsible for passing a
    :class:`FeatureSnapshot` whose ``decision_time_ns <= now_ns`` and
    whose rows all satisfy ``ts <= decision_time_ns`` (the latter is
    enforced by the schema's ``_no_lookahead`` validator).
    """

    def __init__(self, *, root: pathlib.Path | None = None) -> None:
        self._root = root or _default_snapshots_dir()
        # Lazily-populated per-agent set of prediction_ids already on
        # disk, so ``append_if_missing`` doesn't re-scan the file on
        # every call once the set is warm.  Keyed by agent_id.
        self._seen: dict[str, set[str]] = {}

    @property
    def root(self) -> pathlib.Path:
        return self._root

    def _path(self, agent_id: str) -> pathlib.Path:
        _validate_agent_id(agent_id)
        return self._root / f"{agent_id}.jsonl"

    # ------------------------------------------------------------------ #
    # Write                                                              #
    # ------------------------------------------------------------------ #

    def append(self, snapshot: FeatureSnapshot, *, agent_id: str) -> None:
        """Persist a single feature snapshot for ``agent_id``.

        Always appends; no de-duplication.  Use
        :meth:`append_if_missing` for the agent hot path.
        """
        _validate_agent_id(agent_id)
        self._root.mkdir(parents=True, exist_ok=True)
        path = self._path(agent_id)
        # We do not have a prediction_id here; record an empty sidecar
        # so the line shape stays uniform.  ``append_if_missing`` is
        # the path that carries a real prediction_id.
        line = _encode_line("", snapshot)
        # One write call per line.  On POSIX a write smaller than
        # PIPE_BUF (4096) is atomic; snapshots are typically <2KB.  On
        # NTFS a write that fits in one cluster is atomic for our
        # purposes.  We don't use temp-file-then-rename because rename
        # would replace the whole log rather than append a line.
        with path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        # Invalidate the seen-cache for this agent so the next
        # ``append_if_missing`` re-reads the truth from disk.
        self._seen.pop(agent_id, None)

    def append_if_missing(
        self,
        prediction_id: str,
        snapshot: FeatureSnapshot,
        *,
        agent_id: str,
    ) -> bool:
        """Append ``snapshot`` for ``agent_id`` iff ``prediction_id`` not recorded.

        Defensive helper for the agent hot path: call once per cycle
        without coordinating with the reader.  Returns ``True`` if the
        snapshot was appended, ``False`` if it was already present
        (a no-op).

        De-duplication is keyed by ``prediction_id`` (the sidecar
        field on each JSONL line).  The set of seen ids is populated
        lazily from disk on the first call for a given agent and
        invalidated whenever :meth:`append` writes a new line.
        """
        _validate_agent_id(agent_id)
        if not prediction_id:
            raise ValueError("prediction_id must be non-empty")

        seen = self._load_seen(agent_id)
        if prediction_id in seen:
            return False

        self._root.mkdir(parents=True, exist_ok=True)
        path = self._path(agent_id)
        line = _encode_line(prediction_id, snapshot)
        with path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        seen.add(prediction_id)
        return True

    # ------------------------------------------------------------------ #
    # Read                                                               #
    # ------------------------------------------------------------------ #

    def read_for_symbol(
        self,
        symbol: str,
        *,
        agent_id: str,
        since_ns: int | None = None,
        limit: int = 200,
    ) -> list[FeatureSnapshot]:
        """Return the most-recent ``limit`` snapshots for ``symbol``.

        A snapshot matches ``symbol`` if any of its rows carries that
        symbol.  Filters:

          * ``since_ns`` -- only snapshots with
            ``decision_time_ns >= since_ns``.

        We tail the file from the end (matching
        ``prediction_log.py:258-297``) because typical queries want
        "the last N snapshots" and the file may grow large over time.
        Snapshots are returned newest-first by ``decision_time_ns``.
        """
        if not isinstance(symbol, str) or not symbol:
            raise ValueError("symbol must be a non-empty string")
        if limit < 1:
            raise ValueError("limit must be >= 1")
        path = self._path(agent_id)
        if not path.is_file():
            return []

        rows: list[FeatureSnapshot] = []
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                decoded = _decode_line(line)
                if decoded is None:
                    # A malformed line must not take the read down --
                    # skip and continue, matching the tolerance pattern
                    # in prediction_log.py:282-286.
                    continue
                _pid, snapshot = decoded
                if not any(r.symbol == symbol for r in snapshot.rows):
                    continue
                if since_ns is not None and snapshot.decision_time_ns < since_ns:
                    continue
                rows.append(snapshot)

        # Newest-first by decision_time_ns, then truncate.
        rows.sort(key=lambda s: s.decision_time_ns, reverse=True)
        return rows[:limit]

    def read_by_prediction_id(
        self,
        prediction_id: str,
        *,
        agent_id: str,
    ) -> FeatureSnapshot | None:
        """Return the snapshot for a specific prediction_id, or None.

        Scans the agent's snapshot file for the matching prediction_id.
        Returns None if the file doesn't exist or no match is found.
        """
        if not prediction_id:
            return None
        path = self._path(agent_id)
        if not path.is_file():
            return None
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                decoded = _decode_line(line)
                if decoded is None:
                    continue
                pid, snapshot = decoded
                if pid == prediction_id:
                    return snapshot
        return None

    # ------------------------------------------------------------------ #
    # Internal helpers                                                   #
    # ------------------------------------------------------------------ #

    def _load_seen(self, agent_id: str) -> set[str]:
        """Return the (cached) set of prediction_ids already on disk.

        Populated lazily on the first ``append_if_missing`` call for
        an agent, and invalidated by :meth:`append`.  Malformed lines
        are skipped (same tolerance pattern as the read path).
        """
        cached = self._seen.get(agent_id)
        if cached is not None:
            return cached
        seen: set[str] = set()
        path = self._path(agent_id)
        if path.is_file():
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    decoded = _decode_line(line)
                    if decoded is None:
                        continue
                    pid, _snap = decoded
                    if pid:
                        seen.add(pid)
        self._seen[agent_id] = seen
        return seen


__all__ = ["FeatureSnapshotStore"]
