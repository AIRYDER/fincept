"""quant_foundry.ui.job_ledger_view — training job ledger view (T-UI.1).

A pure text/markdown view layer that renders job ledger data for operators.
It shows every relevant job state (queued, running, failed, rejected,
verified, promotion_ineligible, completed) without implying health that the
underlying receipts do not prove.

Design invariants
-----------------
- **Fail-closed on false health.** A row whose ``status`` claims
  ``verified`` but whose ``artifact_verified`` is not ``True`` is a lie the
  UI must refuse to render. :func:`validate_no_false_health` enforces this
  and :meth:`JobLedgerView.render` calls it for every row before drawing.
- **No external UI dependencies.** Rendering is plain text / markdown
  tables so the view can be embedded in a CLI, a log, or a test assertion.
- **Pydantic v2 frozen + extra="forbid"** for both config and row models,
  matching the audit-integrity convention used by ``job_ledger.py``.
- **Operator legibility.** Status is rendered as a bracketed label
  (``[VERIFIED]``, ``[PROMO_INELIGIBLE]``, …) so all seven states are
  visually distinguishable in a monospace table.
"""

from __future__ import annotations

from collections import Counter
from typing import Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = [
    "JobLedgerViewConfig",
    "JobLedgerRow",
    "JobLedgerView",
    "format_bool",
    "format_cost",
    "format_status",
    "validate_no_false_health",
]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: The set of status strings a :class:`JobLedgerRow` may carry. The view
#: layer's status vocabulary is intentionally a *display* vocabulary: it
#: collapses the finer-grained ``JobLedgerState`` transitions (dispatched,
#: runpod_running, callback_received) into the operator-facing labels below.
KNOWN_STATUSES: frozenset[str] = frozenset(
    {
        "queued",
        "running",
        "failed",
        "rejected",
        "verified",
        "promotion_ineligible",
        "completed",
    }
)

#: Statuses that *assert* the artifact has been verified. If a row claims
#: one of these but ``artifact_verified`` is not ``True``, the row is
#: implying health that the receipts do not prove.
_VERIFIED_STATUSES: frozenset[str] = frozenset({"verified", "completed"})

#: Display labels for each status. ``promotion_ineligible`` is abbreviated
#: to ``PROMO_INELIGIBLE`` so the status column stays narrow and all seven
#: states remain visually distinguishable in a monospace table.
_STATUS_LABELS: dict[str, str] = {
    "queued": "[QUEUED]",
    "running": "[RUNNING]",
    "failed": "[FAILED]",
    "rejected": "[REJECTED]",
    "verified": "[VERIFIED]",
    "promotion_ineligible": "[PROMO_INELIGIBLE]",
    "completed": "[COMPLETED]",
}

#: Sort keys accepted by :class:`JobLedgerViewConfig`.
_SORT_KEYS: frozenset[str] = frozenset({"created_at", "status", "cost"})

#: Sort orders accepted by :class:`JobLedgerViewConfig`.
_SORT_ORDERS: frozenset[str] = frozenset({"asc", "desc"})


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def format_status(status: str) -> str:
    """Return a bracketed, upper-case status label for display.

    Examples:
        >>> format_status("queued")
        '[QUEUED]'
        >>> format_status("promotion_ineligible")
        '[PROMO_INELIGIBLE]'
        >>> format_status("nonsense")
        '[UNKNOWN]'

    Args:
        status: A raw status string from a :class:`JobLedgerRow`.

    Returns:
        The bracketed label for known statuses (e.g. ``[QUEUED]``,
        ``[PROMO_INELIGIBLE]``), or ``[UNKNOWN]`` otherwise.
    """
    if not isinstance(status, str) or status not in KNOWN_STATUSES:
        return "[UNKNOWN]"
    return _STATUS_LABELS[status]


def format_cost(cost: float | None) -> str:
    """Format a cost value for display.

    Examples:
        >>> format_cost(1.2345)
        '$1.2345'
        >>> format_cost(None)
        '—'
        >>> format_cost(0.0)
        '$0.0000'

    Args:
        cost: Cost in dollars, or ``None`` when unknown.

    Returns:
        ``$<value>`` with four decimal places, or ``—`` when ``None``.
    """
    if cost is None:
        return "—"
    return f"${cost:.4f}"


def format_bool(value: bool | None) -> str:
    """Format a tri-state boolean for display.

    Examples:
        >>> format_bool(True)
        '✓'
        >>> format_bool(False)
        '✗'
        >>> format_bool(None)
        '—'

    Args:
        value: ``True``, ``False``, or ``None`` (unknown).

    Returns:
        ``✓`` / ``✗`` / ``—``.
    """
    if value is None:
        return "—"
    if value is True:
        return "✓"
    return "✗"


# ---------------------------------------------------------------------------
# False-health validation
# ---------------------------------------------------------------------------


def validate_no_false_health(row: JobLedgerRow) -> bool:
    """Check that a row does not imply health its receipts do not prove.

    The view layer is fail-closed: it refuses to render a row that claims a
    verified / completed state without an actual verified artifact, or that
    claims promotion eligibility without a verified artifact backing it.

    Args:
        row: The :class:`JobLedgerRow` to check.

    Returns:
        ``True`` if the row is honest.

    Raises:
        ValueError: If the UI would imply health that the receipts do not
            prove. The message describes the specific contradiction.
    """
    # 1. A verified/completed status requires artifact_verified=True.
    if row.status in _VERIFIED_STATUSES and row.artifact_verified is not True:
        raise ValueError(
            f"job {row.job_id!r} claims status {row.status!r} but "
            f"artifact_verified is {row.artifact_verified!r} "
            f"(must be True to imply verified health)"
        )

    # 2. Promotion eligibility requires a verified artifact. A job cannot
    #    be promotion-eligible without first being verified — that would
    #    let an unverified artifact reach the leaderboard.
    if row.promotion_eligible is True and row.artifact_verified is not True:
        raise ValueError(
            f"job {row.job_id!r} claims promotion_eligible=True but "
            f"artifact_verified is {row.artifact_verified!r} "
            f"(promotion requires a verified artifact)"
        )

    return True


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class JobLedgerViewConfig(BaseModel):
    """Configuration for :class:`JobLedgerView`.

    Controls which columns are rendered and how rows are sorted. Frozen and
    ``extra="forbid"`` for consistency with the audit-integrity convention.

    Attributes:
        show_cost: Render the cost column.
        show_gpu_type: Render the GPU type column.
        show_artifact_verification: Render the artifact-verified column.
        show_promotion_eligibility: Render the promotion-eligible column.
        show_failure_reason: Render the failure-reason column.
        max_rows: Maximum number of rows to render (>= 1).
        sort_by: Sort key: ``created_at``, ``status``, or ``cost``.
        sort_order: Sort order: ``asc`` or ``desc``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    show_cost: bool = True
    show_gpu_type: bool = True
    show_artifact_verification: bool = True
    show_promotion_eligibility: bool = True
    show_failure_reason: bool = True
    max_rows: int = 100
    sort_by: str = "created_at"
    sort_order: str = "desc"

    @field_validator("max_rows")
    @classmethod
    def _max_rows_ge_one(cls, v: int) -> int:
        if v < 1:
            raise ValueError("max_rows must be >= 1")
        return v

    @field_validator("sort_by")
    @classmethod
    def _sort_by_in_set(cls, v: str) -> str:
        if v not in _SORT_KEYS:
            raise ValueError(
                f"sort_by must be one of {sorted(_SORT_KEYS)}, got {v!r}"
            )
        return v

    @field_validator("sort_order")
    @classmethod
    def _sort_order_in_set(cls, v: str) -> str:
        if v not in _SORT_ORDERS:
            raise ValueError(
                f"sort_order must be one of {sorted(_SORT_ORDERS)}, got {v!r}"
            )
        return v


class JobLedgerRow(BaseModel):
    """One renderable row in the job ledger view.

    Frozen and ``extra="forbid"``. The ``status`` field uses the view
    layer's display vocabulary (see :data:`KNOWN_STATUSES`).

    Attributes:
        job_id: Outbox / ledger job identifier.
        dataset_id: Dataset the job trained on.
        model_family: Model family (e.g. ``xgboost``, ``catboost``).
        runpod_job_id: RunPod job id, or ``None`` if not yet dispatched.
        status: Display status (queued, running, failed, rejected,
            verified, promotion_ineligible, completed).
        gpu_type: GPU type used, or ``None``.
        cost_estimate: Estimated cost in dollars, or ``None``.
        artifact_verified: Whether the artifact was verified, or ``None``
            if verification has not run.
        promotion_eligible: Whether the job is eligible for promotion, or
            ``None`` if not yet determined.
        failure_reason: Human-readable failure reason, or ``None``.
        created_at: ISO-8601 (or comparable) creation timestamp string.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    job_id: str
    dataset_id: str
    model_family: str
    runpod_job_id: str | None = None
    status: str
    gpu_type: str | None = None
    cost_estimate: float | None = None
    artifact_verified: bool | None = None
    promotion_eligible: bool | None = None
    failure_reason: str | None = None
    created_at: str

    @field_validator("job_id", "dataset_id", "model_family", "status", "created_at")
    @classmethod
    def _non_empty_str(cls, v: str) -> str:
        if not isinstance(v, str) or not v:
            raise ValueError("must be a non-empty str")
        return v

    @field_validator("status")
    @classmethod
    def _status_known(cls, v: str) -> str:
        if v not in KNOWN_STATUSES:
            raise ValueError(
                f"status must be one of {sorted(KNOWN_STATUSES)}, got {v!r}"
            )
        return v

    @field_validator("cost_estimate")
    @classmethod
    def _cost_non_negative(cls, v: float | None) -> float | None:
        if v is not None and v < 0:
            raise ValueError("cost_estimate must be >= 0")
        return v


# ---------------------------------------------------------------------------
# View
# ---------------------------------------------------------------------------


class JobLedgerView:
    """Render job ledger rows for operators (pure text/markdown).

    The view is fail-closed on false health: every row is run through
    :func:`validate_no_false_health` before it is rendered, so a row that
    claims a verified state without a verified artifact raises rather than
    silently misleading an operator.

    Example:
        >>> cfg = JobLedgerViewConfig()
        >>> view = JobLedgerView(cfg)
        >>> rows = [JobLedgerRow(
        ...     job_id="j1", dataset_id="d1", model_family="xgboost",
        ...     status="queued", created_at="2026-01-01T00:00:00Z",
        ... )]
        >>> table = view.render(rows)
        >>> "[QUEUED]" in table
        True
    """

    def __init__(self, config: JobLedgerViewConfig) -> None:
        """Initialize the view with a frozen config.

        Args:
            config: The :class:`JobLedgerViewConfig` controlling columns
                and sorting.
        """
        self.config = config

    # --- public API --------------------------------------------------------

    def render(self, rows: Sequence[JobLedgerRow]) -> str:
        """Render a markdown-style table of job rows.

        Columns are selected by the config. Every row is validated for
        false health before rendering (fail-closed). Rows are sorted and
        truncated to ``max_rows``.

        Args:
            rows: The rows to render.

        Returns:
            A formatted string containing a header, a separator, and one
            line per row. An empty input yields a header-only table with a
            ``(no jobs)`` placeholder line.

        Raises:
            ValueError: If any row implies false health (via
                :func:`validate_no_false_health`).
        """
        # Fail-closed: validate every row before drawing anything.
        for row in rows:
            validate_no_false_health(row)

        sorted_rows = self.sort_rows(list(rows))[: self.config.max_rows]

        headers: list[str] = ["JOB_ID", "STATUS", "DATASET", "MODEL"]
        if self.config.show_gpu_type:
            headers.append("GPU")
        if self.config.show_cost:
            headers.append("COST")
        if self.config.show_artifact_verification:
            headers.append("VERIFIED")
        if self.config.show_promotion_eligibility:
            headers.append("PROMO")
        if self.config.show_failure_reason:
            headers.append("FAILURE_REASON")
        headers.append("CREATED_AT")

        lines: list[str] = []
        lines.append("| " + " | ".join(headers) + " |")
        lines.append("| " + " | ".join("---" for _ in headers) + " |")

        if not sorted_rows:
            lines.append("| " + " | ".join("(no jobs)" for _ in headers) + " |")
            return "\n".join(lines)

        for row in sorted_rows:
            cells: list[str] = [row.job_id, format_status(row.status), row.dataset_id, row.model_family]
            if self.config.show_gpu_type:
                cells.append(row.gpu_type if row.gpu_type else "—")
            if self.config.show_cost:
                cells.append(format_cost(row.cost_estimate))
            if self.config.show_artifact_verification:
                cells.append(format_bool(row.artifact_verified))
            if self.config.show_promotion_eligibility:
                cells.append(format_bool(row.promotion_eligible))
            if self.config.show_failure_reason:
                cells.append(row.failure_reason if row.failure_reason else "—")
            cells.append(row.created_at)
            lines.append("| " + " | ".join(cells) + " |")

        return "\n".join(lines)

    def render_summary(self, rows: Sequence[JobLedgerRow]) -> str:
        """Render a one-block summary of the job ledger.

        Includes total jobs, a per-status breakdown, total estimated cost,
        verified count, and promotion-eligible count. Every row is
        validated for false health first (fail-closed).

        Args:
            rows: The rows to summarize.

        Returns:
            A formatted multi-line summary string.

        Raises:
            ValueError: If any row implies false health.
        """
        for row in rows:
            validate_no_false_health(row)

        total = len(rows)
        status_counts = Counter(row.status for row in rows)
        total_cost = sum(
            (r.cost_estimate or 0.0) for r in rows
        )
        verified_count = sum(
            1 for r in rows if r.artifact_verified is True
        )
        promo_count = sum(
            1 for r in rows if r.promotion_eligible is True
        )

        lines: list[str] = []
        lines.append("=== Job Ledger Summary ===")
        lines.append(f"Total jobs: {total}")
        lines.append("By status:")
        # Deterministic order: known statuses first, then any leftovers.
        for status in sorted(KNOWN_STATUSES):
            count = status_counts.get(status, 0)
            lines.append(f"  {format_status(status)} {status}: {count}")
        # Any unknown statuses (shouldn't happen given validation, but be safe).
        for status, count in sorted(status_counts.items()):
            if status not in KNOWN_STATUSES:
                lines.append(f"  [UNKNOWN] {status}: {count}")
        lines.append(f"Total estimated cost: {format_cost(total_cost if total else None)}")
        lines.append(f"Verified artifacts: {verified_count}")
        lines.append(f"Promotion-eligible: {promo_count}")
        return "\n".join(lines)

    def render_job_detail(self, row: JobLedgerRow) -> str:
        """Render a detailed, key-value view of a single job.

        Args:
            row: The :class:`JobLedgerRow` to render.

        Returns:
            A formatted multi-line string with one labelled line per field.

        Raises:
            ValueError: If the row implies false health.
        """
        validate_no_false_health(row)

        lines: list[str] = []
        lines.append(f"=== Job Detail: {row.job_id} ===")
        lines.append(f"Status:           {format_status(row.status)}")
        lines.append(f"Dataset:          {row.dataset_id}")
        lines.append(f"Model family:     {row.model_family}")
        lines.append(f"RunPod job id:    {row.runpod_job_id if row.runpod_job_id else '—'}")
        lines.append(f"GPU type:         {row.gpu_type if row.gpu_type else '—'}")
        lines.append(f"Cost estimate:    {format_cost(row.cost_estimate)}")
        lines.append(f"Artifact verified:{format_bool(row.artifact_verified)}")
        lines.append(f"Promotion eligible:{format_bool(row.promotion_eligible)}")
        lines.append(f"Failure reason:   {row.failure_reason if row.failure_reason else '—'}")
        lines.append(f"Created at:       {row.created_at}")
        return "\n".join(lines)

    def filter_by_status(
        self, rows: Sequence[JobLedgerRow], status: str
    ) -> list[JobLedgerRow]:
        """Return only rows whose ``status`` equals ``status``.

        Args:
            rows: The rows to filter.
            status: The status string to match (e.g. ``"verified"``).

        Returns:
            A list of matching rows (preserving input order). If
            ``status`` is not a known status, an empty list is returned
            (no row can match an unknown status).
        """
        if status not in KNOWN_STATUSES:
            return []
        return [row for row in rows if row.status == status]

    def sort_rows(self, rows: Sequence[JobLedgerRow]) -> list[JobLedgerRow]:
        """Sort rows by the configured key and order.

        Sorting is stable and does not mutate the input sequence.

        Args:
            rows: The rows to sort.

        Returns:
            A new list sorted by ``config.sort_by`` in
            ``config.sort_order``.
        """
        items = list(rows)
        key = self.config.sort_by
        reverse = self.config.sort_order == "desc"

        if key == "created_at":
            return sorted(items, key=lambda r: r.created_at, reverse=reverse)
        if key == "status":
            return sorted(items, key=lambda r: r.status, reverse=reverse)
        if key == "cost":
            return sorted(
                items,
                key=lambda r: (r.cost_estimate if r.cost_estimate is not None else -1.0),
                reverse=reverse,
            )
        # Unreachable: config validation restricts sort_by.
        return items
