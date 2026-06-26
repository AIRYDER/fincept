"""
quant_foundry.budget — hard monthly budget guard for heavy jobs (TASK-0901).

GPU spend must fail closed, exactly like the JWT runtime guard. A job that
would exceed the monthly budget ceiling is rejected BEFORE it starts, not
after it has already incurred cost. This is the cost-governance invariant
from cross-cutting rigor §4: "GPU spend must fail closed."

Design:
- ``BudgetGuard`` tracks cumulative monthly spend in a JSONL file (durable
  across restarts — a process restart must not reset the spend counter).
- ``check_and_reserve(amount_cents, job_type)`` returns a ``BudgetDecision``
  with ``allowed=True/False``. If allowed, the amount is reserved (added to
  the monthly total). If not allowed, the job is rejected before it starts.
- ``record_spend(amount_cents, job_type)`` records actual spend (e.g. after a
  RunPod job completes and the real cost is known). This can adjust a prior
  reservation up or down.
- The monthly ceiling is configurable via the constructor (the gateway reads
  it from ``QUANT_FOUNDRY_MONTHLY_BUDGET_CENTS`` env var).
- A global kill switch (``set_kill_switch(True)``) blocks ALL paid jobs
  regardless of remaining budget — for manual emergency stops. Zero-cost jobs
  (amount=0, e.g. local mock) are always allowed.

File-disjoint from all active builders. New module, no imports of settlement /
dossier / tournament / gateway / outbox / inbox. The gateway can optionally
use this guard by injecting it into ``create_job``.
"""

from __future__ import annotations

import dataclasses
import json
import os
import pathlib
import time
from typing import Any


@dataclasses.dataclass(frozen=True)
class BudgetDecision:
    """The result of a ``check_and_reserve`` call.

    - ``allowed``: True if the job may proceed; False if it is rejected.
    - ``reason``: human-readable explanation (empty when allowed).
    - ``job_type``: the job type that was checked.
    - ``amount_cents``: the amount that was requested.
    - ``monthly_budget_cents``: the configured monthly ceiling.
    - ``spent_cents``: cumulative spend in the current month (including this
      reservation if allowed).
    - ``remaining_cents``: remaining budget in the current month (after this
      reservation if allowed).
    - ``year_month``: the YYYY-MM string for the current billing period.
    """

    allowed: bool
    reason: str
    job_type: str
    amount_cents: int
    monthly_budget_cents: int
    spent_cents: int
    remaining_cents: int
    year_month: str


@dataclasses.dataclass(frozen=True)
class BudgetSummary:
    """Read-only summary of the current budget state."""

    monthly_budget_cents: int
    spent_cents: int
    remaining_cents: int
    kill_switch_enabled: bool
    year_month: str


# --------------------------------------------------------------------------- #
# BudgetGuard                                                                  #
# --------------------------------------------------------------------------- #


class BudgetGuard:
    """Hard monthly budget guard with durable spend tracking.

    The spend ledger is a JSONL file at ``<base_dir>/spend_<YYYY-MM>.jsonl``.
    Each line is a JSON object with ``ts_unix``, ``job_type``, ``amount_cents``,
    and ``kind`` (``reserve`` or ``record``). The file is append-only; the
    monthly total is computed by reading all lines for the current month.

    This design is restart-safe: a new ``BudgetGuard`` pointing at the same
    ``base_dir`` will see the same cumulative spend as the previous one.
    """

    def __init__(
        self,
        *,
        base_dir: pathlib.Path | str,
        monthly_budget_cents: int = 0,
        kill_switch_enabled: bool = False,
    ) -> None:
        self.base_dir = pathlib.Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.monthly_budget_cents = monthly_budget_cents
        self._kill_switch = kill_switch_enabled

    # --- public API -------------------------------------------------------- #

    def check_and_reserve(
        self,
        *,
        amount_cents: int,
        job_type: str,
    ) -> BudgetDecision:
        """Check whether a job may proceed and reserve the amount if allowed.

        Fail-closed: if the amount would exceed the monthly budget, the job
        is rejected and NO amount is reserved. If the kill switch is active,
        all paid jobs (amount > 0) are rejected.

        Args:
            amount_cents: the estimated cost in cents (1 USD = 100 cents).
                Must be >= 0. Use 0 for zero-cost jobs (local mock, tests).
            job_type: the job type (e.g. "training", "inference", "mock").

        Returns:
            ``BudgetDecision`` with ``allowed=True`` if the job may proceed.
        """
        if amount_cents < 0:
            raise ValueError(f"amount_cents must be >= 0; got {amount_cents}")

        year_month = _current_year_month()
        spent = self._read_monthly_spend(year_month)
        remaining = self.monthly_budget_cents - spent

        # Kill switch: blocks ALL paid jobs.
        if self._kill_switch and amount_cents > 0:
            return BudgetDecision(
                allowed=False,
                reason="budget kill switch is active; all paid jobs are blocked",
                job_type=job_type,
                amount_cents=amount_cents,
                monthly_budget_cents=self.monthly_budget_cents,
                spent_cents=spent,
                remaining_cents=remaining,
                year_month=year_month,
            )

        # Zero-cost jobs are always allowed (even with zero budget).
        if amount_cents == 0:
            return BudgetDecision(
                allowed=True,
                reason="",
                job_type=job_type,
                amount_cents=0,
                monthly_budget_cents=self.monthly_budget_cents,
                spent_cents=spent,
                remaining_cents=remaining,
                year_month=year_month,
            )

        # Budget check: fail-closed if the amount would exceed the ceiling.
        if amount_cents > remaining:
            return BudgetDecision(
                allowed=False,
                reason=(
                    f"job cost {amount_cents}c exceeds monthly budget: "
                    f"spent {spent}c + requested {amount_cents}c > "
                    f"ceiling {self.monthly_budget_cents}c "
                    f"(remaining {remaining}c)"
                ),
                job_type=job_type,
                amount_cents=amount_cents,
                monthly_budget_cents=self.monthly_budget_cents,
                spent_cents=spent,
                remaining_cents=remaining,
                year_month=year_month,
            )

        # Allowed: reserve the amount by appending to the ledger.
        self._append_ledger(
            year_month=year_month,
            job_type=job_type,
            amount_cents=amount_cents,
            kind="reserve",
        )
        new_spent = spent + amount_cents
        new_remaining = self.monthly_budget_cents - new_spent
        return BudgetDecision(
            allowed=True,
            reason="",
            job_type=job_type,
            amount_cents=amount_cents,
            monthly_budget_cents=self.monthly_budget_cents,
            spent_cents=new_spent,
            remaining_cents=new_remaining,
            year_month=year_month,
        )

    def record_spend(
        self,
        *,
        amount_cents: int,
        job_type: str,
        year_month: str | None = None,
    ) -> None:
        """Record actual spend (e.g. after a RunPod job completes).

        This is additive — it increases the monthly total by ``amount_cents``.
        Use a negative amount to adjust a prior over-reservation downward.
        """
        if amount_cents < 0:
            raise ValueError(f"amount_cents must be >= 0; got {amount_cents}")
        ym = year_month or _current_year_month()
        self._append_ledger(
            year_month=ym,
            job_type=job_type,
            amount_cents=amount_cents,
            kind="record",
        )

    def get_monthly_spend(self, year_month: str | None = None) -> int:
        """Return the cumulative spend for the given month (default: current)."""
        ym = year_month or _current_year_month()
        return self._read_monthly_spend(ym)

    def get_summary(self) -> dict[str, Any]:
        """Return a read-only summary of the current budget state."""
        ym = _current_year_month()
        spent = self._read_monthly_spend(ym)
        return {
            "monthly_budget_cents": self.monthly_budget_cents,
            "spent_cents": spent,
            "remaining_cents": self.monthly_budget_cents - spent,
            "kill_switch_enabled": self._kill_switch,
            "year_month": ym,
        }

    def set_kill_switch(self, enabled: bool) -> None:
        """Toggle the budget kill switch (blocks all paid jobs when active)."""
        self._kill_switch = enabled

    # --- internal helpers -------------------------------------------------- #

    def _ledger_path(self, year_month: str) -> pathlib.Path:
        return self.base_dir / f"spend_{year_month}.jsonl"

    def _append_ledger(
        self,
        *,
        year_month: str,
        job_type: str,
        amount_cents: int,
        kind: str,
    ) -> None:
        """Append a spend record to the monthly ledger (durable)."""
        entry = {
            "ts_unix": int(time.time()),
            "job_type": job_type,
            "amount_cents": amount_cents,
            "kind": kind,
        }
        line = json.dumps(entry, separators=(",", ":"), sort_keys=True)
        with open(self._ledger_path(year_month), "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def _read_monthly_spend(self, year_month: str) -> int:
        """Read the cumulative spend for the given month from the ledger."""
        path = self._ledger_path(year_month)
        if not path.exists():
            return 0
        total = 0
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    total += int(entry.get("amount_cents", 0))
                except (json.JSONDecodeError, ValueError, TypeError):
                    # Skip malformed lines (defensive — a corrupt ledger
                    # should not crash the budget guard).
                    continue
        return total


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #


def _current_year_month() -> str:
    """Return the current YYYY-MM string (UTC)."""
    t = time.gmtime()
    return f"{t.tm_year:04d}-{t.tm_mon:02d}"


def from_env(base_dir: pathlib.Path | str) -> BudgetGuard:
    """Build a ``BudgetGuard`` from environment variables.

    Env vars:
    - ``QUANT_FOUNDRY_MONTHLY_BUDGET_CENTS``: monthly ceiling in cents
      (default: 0 = no paid jobs allowed until a budget is set).
    - ``QUANT_FOUNDRY_BUDGET_KILL_SWITCH``: "true"/"false" (default: "false").
    """
    budget_str = os.environ.get("QUANT_FOUNDRY_MONTHLY_BUDGET_CENTS", "0")
    try:
        monthly_budget = int(budget_str)
    except ValueError:
        monthly_budget = 0
    kill_str = os.environ.get("QUANT_FOUNDRY_BUDGET_KILL_SWITCH", "false")
    kill_switch = kill_str.strip().lower() in ("true", "1", "yes")
    return BudgetGuard(
        base_dir=base_dir,
        monthly_budget_cents=monthly_budget,
        kill_switch_enabled=kill_switch,
    )
