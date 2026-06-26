"""
quant_foundry.registry — durable, immutable dossier registry (TASK-0403).

The registry is the persistent home for ``DossierRecord``s. It is:

- **Durable**: append-only JSONL under ``<base_dir>/dossier_registry.jsonl``;
  restart-safe (replays all lines, last record per model_id wins).
- **Immutable by version/hash**: registering the same dossier (same ``model_id``
  + same ``content_hash``) is idempotent (returns the existing record). Registering
  the same ``model_id`` with a DIFFERENT ``content_hash`` is a security event
  (ValueError) — a dossier cannot be silently swapped under a known model id.
  This mirrors the outbox/inbox diff-hash rejection invariant (TASK-0304).
- **Append-only blocking_issues**: the sentinel (TASK-0406) and tournament
  (TASK-0404) append blocking issues; the list is never truncated.
- **Read API**: ``get(model_id)``, ``get_by_hash(content_hash)``, ``list(status=)``.
  The API route (``services/api/routes/quant_foundry.py``) is owned by TASK-0306;
  this module exposes a Python read API only for MVP.

File-disjoint from all active builders (see BUILDER3.md). Does NOT import
``settlement.py`` / ``shadow_ledger.py`` / ``feature_lake.py`` — dossiers reference
evidence by id/ref, not by code coupling.
"""

from __future__ import annotations

import json
import pathlib
import time
from typing import Any

from quant_foundry.dossier import DossierRecord, DossierStatus


class DossierRegistry:
    """Filesystem JSONL-backed dossier registry. One process, one writer per file.

    The file path is ``<base_dir>/dossier_registry.jsonl``. Each line is the full
    ``DossierRecord`` JSON at the moment of write. Reload replays all lines and
    keeps the last record per ``model_id`` (last-writer-wins by file order).
    """

    FILENAME = "dossier_registry.jsonl"

    def __init__(self, base_dir: pathlib.Path | str) -> None:
        self.base_dir = pathlib.Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.base_dir / self.FILENAME
        self._records: dict[str, DossierRecord] = {}
        self._by_hash: dict[str, str] = {}  # content_hash -> model_id
        self._reload()

    # --- durability ---

    def _reload(self) -> None:
        """Replay JSONL from disk. Last line per model_id wins."""
        self._records = {}
        self._by_hash = {}
        if not self.path.is_file():
            return
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                data = json.loads(line)
                rec = DossierRecord.model_validate(data)
                self._records[rec.model_id] = rec
                self._by_hash[rec.content_hash] = rec.model_id

    def _append(self, rec: DossierRecord) -> None:
        """Append one record line and flush for durability."""
        line = rec.model_dump_json()
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
            try:
                import os

                os.fsync(fh.fileno())
            except (OSError, AttributeError):
                # fsync best-effort on platforms that don't support it.
                pass

    # --- public API ---

    def register(self, dossier: DossierRecord) -> DossierRecord:
        """Register a dossier idempotently.

        - Same ``model_id`` + same ``content_hash`` -> returns the existing record
          (idempotent, no new write).
        - Same ``model_id`` + DIFFERENT ``content_hash`` -> ValueError (security
          event: a dossier cannot be silently swapped under a known model id).
        - New ``model_id`` -> appends and indexes.

        The registered record gets ``registered_at_ns`` stamped on first registration.
        """
        existing = self._records.get(dossier.model_id)
        if existing is not None:
            if existing.content_hash != dossier.content_hash:
                raise ValueError(
                    f"content hash mismatch for existing model_id {dossier.model_id}: "
                    "dossier is immutable by version/hash (possible tamper / replay — "
                    "security event)"
                )
            # Idempotent re-register: return existing record, no new write.
            return existing

        # Stamp registered_at_ns on first registration (frozen model -> model_copy).
        if dossier.registered_at_ns is None:
            dossier = dossier.model_copy(update={"registered_at_ns": time.time_ns()})

        self._append(dossier)
        self._records[dossier.model_id] = dossier
        self._by_hash[dossier.content_hash] = dossier.model_id
        return dossier

    def get(self, model_id: str) -> DossierRecord | None:
        """Return the latest dossier for ``model_id``, or None."""
        return self._records.get(model_id)

    def get_by_hash(self, content_hash: str) -> DossierRecord | None:
        """Return the dossier with the given ``content_hash``, or None."""
        model_id = self._by_hash.get(content_hash)
        if model_id is None:
            return None
        return self._records.get(model_id)

    def list(self, *, status: DossierStatus | None = None) -> list[DossierRecord]:
        """List dossiers, optionally filtered by status. Insertion order."""
        records = list(self._records.values())
        if status is not None:
            records = [r for r in records if r.status == status]
        return records

    def add_blocking_issue(
        self,
        model_id: str,
        *,
        source: str,
        code: str,
        note: str | None = None,
    ) -> DossierRecord:
        """Append a blocking issue to a dossier's ``blocking_issues`` list.

        Blocking issues are append-only and visible. A blocking issue is a hard
        gate on promotion (the promotion review queue, TASK-0702, refuses to
        override without an explicit, recorded human waiver).

        Raises ``KeyError`` if ``model_id`` is unknown.
        """
        existing = self._records.get(model_id)
        if existing is None:
            raise KeyError(f"unknown model_id: {model_id}")

        issue: dict[str, Any] = {
            "source": source,
            "code": code,
            "ts_ns": time.time_ns(),
        }
        if note is not None:
            issue["note"] = note

        new_issues = [*existing.blocking_issues, issue]
        updated = existing.model_copy(update={"blocking_issues": new_issues})
        self._append(updated)
        self._records[model_id] = updated
        # content_hash unchanged (blocking_issues excluded from content hash) —
        # no need to update _by_hash.
        return updated

    def update_status(self, model_id: str, status: DossierStatus) -> DossierRecord:
        existing = self._records.get(model_id)
        if existing is None:
            raise KeyError(f"unknown model_id: {model_id}")

        updated = existing.model_copy(update={"status": status})
        self._append(updated)
        self._records[model_id] = updated
        self._by_hash[updated.content_hash] = model_id
        return updated
