"""Dataset registry view layer for operators (T-UI.2).

Renders dataset registry data so operators can see readiness levels,
quality gates, and *why* a dataset cannot be production-trained.

Design principles
-----------------
* **Fail-closed**: the UI must never imply production readiness that the
  underlying receipts (manifest hash, quality gate, upload status) do not
  prove.  ``validate_no_false_readiness`` enforces this invariant.
* **No external UI deps**: pure text/markdown rendering only.
* **Pydantic v2 strict models**: ``frozen=True, extra="forbid"`` so
  config and rows are immutable and reject unknown fields.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Ordered readiness levels from raw (L0) to golden (L4).
READINESS_LEVELS: list[str] = [
    "L0_raw",
    "L1_cleaned",
    "L2_validated",
    "L3_production",
    "L4_golden",
]

#: Numeric rank for each readiness level (higher = more proven).
_READINESS_RANK: dict[str, int] = {lvl: i for i, lvl in enumerate(READINESS_LEVELS)}

#: Minimum readiness rank required for production training.
_PRODUCTION_MIN_RANK: int = _READINESS_RANK["L3_production"]

#: Valid quality-gate statuses.
QUALITY_GATE_STATUSES: list[str | None] = [
    "passed",
    "failed",
    "pending",
    "not_run",
    None,
]

#: Valid upload statuses.
UPLOAD_STATUSES: list[str | None] = [
    "staged",
    "uploaded",
    "verified",
    "failed",
    None,
]

#: Valid training modes.
ELIGIBLE_MODES: list[str] = ["canary", "research", "production"]

#: Valid sort-by fields.
_SORT_BY_FIELDS: list[str] = ["dataset_id", "readiness_level", "created_at"]

#: Valid sort orders.
_SORT_ORDERS: list[str] = ["asc", "desc"]


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class DatasetRegistryViewConfig(BaseModel):
    """Configuration for :class:`DatasetRegistryView`.

    Controls which columns are rendered and how rows are sorted.

    Attributes
    ----------
    show_manifest_hash:
        Show the manifest-hash column.
    show_quality_gate:
        Show the quality-gate status column.
    show_upload_status:
        Show the upload-status column.
    show_eligible_modes:
        Show the eligible-modes column.
    show_readiness_level:
        Show the readiness-level column.
    max_rows:
        Maximum number of rows to render (must be >= 1).
    sort_by:
        Field to sort rows by (``dataset_id``, ``readiness_level``,
        ``created_at``).
    sort_order:
        Sort direction (``asc`` or ``desc``).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    show_manifest_hash: bool = True
    show_quality_gate: bool = True
    show_upload_status: bool = True
    show_eligible_modes: bool = True
    show_readiness_level: bool = True
    max_rows: int = 100
    sort_by: str = "dataset_id"
    sort_order: str = "asc"

    @field_validator("max_rows")
    @classmethod
    def _max_rows_must_be_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError("max_rows must be >= 1")
        return v

    @field_validator("sort_by")
    @classmethod
    def _sort_by_must_be_valid(cls, v: str) -> str:
        if v not in _SORT_BY_FIELDS:
            raise ValueError(f"sort_by must be one of {_SORT_BY_FIELDS}, got {v!r}")
        return v

    @field_validator("sort_order")
    @classmethod
    def _sort_order_must_be_valid(cls, v: str) -> str:
        if v not in _SORT_ORDERS:
            raise ValueError(f"sort_order must be one of {_SORT_ORDERS}, got {v!r}")
        return v


class DatasetRegistryRow(BaseModel):
    """A single dataset row rendered by the registry view.

    Attributes
    ----------
    dataset_id:
        Unique identifier for the dataset.
    readiness_level:
        One of ``L0_raw``, ``L1_cleaned``, ``L2_validated``,
        ``L3_production``, ``L4_golden``.
    manifest_hash:
        Content hash of the dataset manifest (``None`` if not computed).
    quality_gate_status:
        Quality-gate result (``passed``, ``failed``, ``pending``,
        ``not_run``, or ``None``).
    upload_status:
        Upload state (``staged``, ``uploaded``, ``verified``,
        ``failed``, or ``None``).
    eligible_modes:
        Training modes the dataset is eligible for.
    created_at:
        ISO-8601 creation timestamp.
    blocking_reasons:
        Reasons why the dataset cannot be production-trained.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    dataset_id: str
    readiness_level: str
    manifest_hash: str | None = None
    quality_gate_status: str | None = None
    upload_status: str | None = None
    eligible_modes: list[str] = Field(default_factory=list)
    created_at: str = ""
    blocking_reasons: list[str] = Field(default_factory=list)

    @field_validator("readiness_level")
    @classmethod
    def _readiness_level_must_be_valid(cls, v: str) -> str:
        if v not in READINESS_LEVELS:
            raise ValueError(f"readiness_level must be one of {READINESS_LEVELS}, got {v!r}")
        return v

    @field_validator("quality_gate_status")
    @classmethod
    def _quality_gate_status_must_be_valid(cls, v: str | None) -> str | None:
        if v is not None and v not in ["passed", "failed", "pending", "not_run"]:
            raise ValueError(
                f"quality_gate_status must be one of "
                f"['passed', 'failed', 'pending', 'not_run', None], got {v!r}"
            )
        return v

    @field_validator("upload_status")
    @classmethod
    def _upload_status_must_be_valid(cls, v: str | None) -> str | None:
        if v is not None and v not in ["staged", "uploaded", "verified", "failed"]:
            raise ValueError(
                f"upload_status must be one of "
                f"['staged', 'uploaded', 'verified', 'failed', None], got {v!r}"
            )
        return v

    @field_validator("eligible_modes")
    @classmethod
    def _eligible_modes_must_be_valid(cls, v: list[str]) -> list[str]:
        for mode in v:
            if mode not in ELIGIBLE_MODES:
                raise ValueError(
                    f"eligible_modes entries must be one of {ELIGIBLE_MODES}, got {mode!r}"
                )
        return v


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def format_readiness(level: str) -> str:
    """Return a bracketed readiness badge.

    Examples
    --------
    >>> format_readiness("L3_production")
    '[L3]'
    >>> format_readiness("L0_raw")
    '[L0]'
    """
    if not level or "_" not in level:
        return "[??]"
    prefix = level.split("_", 1)[0]
    return f"[{prefix}]"


def format_quality_gate(status: str | None) -> str:
    """Return a bracketed quality-gate badge.

    Mapping: ``passed`` -> ``[PASS]``, ``failed`` -> ``[FAIL]``,
    ``pending`` -> ``[PEND]``, ``not_run`` -> ``[—]``, ``None`` -> ``—``.
    """
    mapping: dict[str | None, str] = {
        "passed": "[PASS]",
        "failed": "[FAIL]",
        "pending": "[PEND]",
        "not_run": "[—]",
        None: "—",
    }
    return mapping.get(status, "—")


def format_upload_status(status: str | None) -> str:
    """Return a bracketed upload-status badge.

    Mapping: ``staged`` -> ``[STAGED]``, ``uploaded`` -> ``[UPLOADED]``,
    ``verified`` -> ``[VERIFIED]``, ``failed`` -> ``[FAILED]``,
    ``None`` -> ``—``.
    """
    mapping: dict[str | None, str] = {
        "staged": "[STAGED]",
        "uploaded": "[UPLOADED]",
        "verified": "[VERIFIED]",
        "failed": "[FAILED]",
        None: "—",
    }
    return mapping.get(status, "—")


# ---------------------------------------------------------------------------
# Blocking-reason logic (fail-closed)
# ---------------------------------------------------------------------------


def _readiness_rank(level: str) -> int:
    """Return the numeric rank of a readiness level (0-4)."""
    return _READINESS_RANK.get(level, -1)


def get_blocking_reasons(row: DatasetRegistryRow) -> list[str]:
    """Return reasons why *row* cannot be production-trained.

    Checks (fail-closed):
    * readiness level < L3_production
    * quality gate not ``passed``
    * upload status not ``verified``
    * manifest hash missing

    Returns an empty list when the dataset is production-eligible.
    """
    reasons: list[str] = []

    if _readiness_rank(row.readiness_level) < _PRODUCTION_MIN_RANK:
        reasons.append(f"readiness_level {row.readiness_level} below L3_production")

    if row.quality_gate_status != "passed":
        reasons.append(f"quality_gate_status {row.quality_gate_status!r} not 'passed'")

    if row.upload_status != "verified":
        reasons.append(f"upload_status {row.upload_status!r} not 'verified'")

    if not row.manifest_hash:
        reasons.append("manifest_hash missing")

    return reasons


def validate_no_false_readiness(row: DatasetRegistryRow) -> bool:
    """Check that *row* does not imply unproven production readiness.

    A row is *dishonest* when it lists ``"production"`` in
    ``eligible_modes`` but the receipts do not back that claim — i.e.
    readiness < L3 or quality gate not ``passed``.

    Returns
    -------
    bool
        ``True`` if the row is honest.

    Raises
    ------
    ValueError
        If the UI would imply readiness that the receipts do not prove.
    """
    claims_production = "production" in row.eligible_modes
    proven = (
        _readiness_rank(row.readiness_level) >= _PRODUCTION_MIN_RANK
        and row.quality_gate_status == "passed"
    )
    if claims_production and not proven:
        raise ValueError(
            f"dataset {row.dataset_id!r} claims 'production' in "
            f"eligible_modes but receipts do not prove it "
            f"(readiness_level={row.readiness_level!r}, "
            f"quality_gate_status={row.quality_gate_status!r})"
        )
    return True


# ---------------------------------------------------------------------------
# View
# ---------------------------------------------------------------------------


class DatasetRegistryView:
    """Render dataset registry data for operators.

    Parameters
    ----------
    config:
        A :class:`DatasetRegistryViewConfig` controlling columns and
        sorting.
    """

    def __init__(self, config: DatasetRegistryViewConfig) -> None:
        self.config = config

    # -- rendering -------------------------------------------------------

    def render(self, rows: list[DatasetRegistryRow]) -> str:
        """Render a table of dataset rows.

        Columns are included/excluded based on ``self.config``.
        The table is truncated to ``max_rows``.
        """
        sorted_rows = self.sort_rows(rows)[: self.config.max_rows]

        headers: list[str] = ["Dataset ID"]
        if self.config.show_readiness_level:
            headers.append("Readiness")
        if self.config.show_manifest_hash:
            headers.append("Manifest Hash")
        if self.config.show_quality_gate:
            headers.append("Quality Gate")
        if self.config.show_upload_status:
            headers.append("Upload")
        if self.config.show_eligible_modes:
            headers.append("Eligible Modes")

        lines: list[str] = []
        sep = " | "
        lines.append(sep.join(headers))
        lines.append("-+-".join("-" * len(h) for h in headers))

        if not sorted_rows:
            lines.append("(no datasets)")
            return "\n".join(lines)

        for row in sorted_rows:
            cells: list[str] = [row.dataset_id]
            if self.config.show_readiness_level:
                cells.append(format_readiness(row.readiness_level))
            if self.config.show_manifest_hash:
                cells.append(row.manifest_hash or "—")
            if self.config.show_quality_gate:
                cells.append(format_quality_gate(row.quality_gate_status))
            if self.config.show_upload_status:
                cells.append(format_upload_status(row.upload_status))
            if self.config.show_eligible_modes:
                cells.append(",".join(row.eligible_modes) if row.eligible_modes else "—")
            lines.append(sep.join(cells))

        return "\n".join(lines)

    def render_summary(self, rows: list[DatasetRegistryRow]) -> str:
        """Render a summary of the dataset registry.

        Includes total datasets, counts by readiness level, counts by
        quality-gate status, and the production-eligible count.
        """
        total = len(rows)

        by_readiness: dict[str, int] = {lvl: 0 for lvl in READINESS_LEVELS}
        for row in rows:
            by_readiness[row.readiness_level] = by_readiness.get(row.readiness_level, 0) + 1

        by_quality: dict[str, int] = {}
        for row in rows:
            key = row.quality_gate_status or "none"
            by_quality[key] = by_quality.get(key, 0) + 1

        production_eligible = len(self.filter_production_eligible(rows))

        lines: list[str] = []
        lines.append("Dataset Registry Summary")
        lines.append("=" * 24)
        lines.append(f"Total datasets: {total}")
        lines.append("")
        lines.append("By readiness level:")
        for lvl in READINESS_LEVELS:
            lines.append(f"  {format_readiness(lvl)} {lvl}: {by_readiness.get(lvl, 0)}")
        lines.append("")
        lines.append("By quality gate:")
        for status in ["passed", "failed", "pending", "not_run", "none"]:
            lines.append(f"  {status}: {by_quality.get(status, 0)}")
        lines.append("")
        lines.append(f"Production-eligible: {production_eligible}")
        return "\n".join(lines)

    def render_dataset_detail(self, row: DatasetRegistryRow) -> str:
        """Render a detailed view of a single dataset row.

        Includes all fields plus blocking reasons (computed live via
        :func:`get_blocking_reasons`).
        """
        reasons = get_blocking_reasons(row)
        eligible = "production" in row.eligible_modes

        lines: list[str] = []
        lines.append(f"Dataset Detail: {row.dataset_id}")
        lines.append("=" * (16 + len(row.dataset_id)))
        lines.append(
            f"  Readiness Level : {format_readiness(row.readiness_level)} {row.readiness_level}"
        )
        lines.append(f"  Manifest Hash   : {row.manifest_hash or '—'}")
        lines.append(
            f"  Quality Gate    : {format_quality_gate(row.quality_gate_status)} {row.quality_gate_status or '—'}"
        )
        lines.append(
            f"  Upload Status   : {format_upload_status(row.upload_status)} {row.upload_status or '—'}"
        )
        lines.append(
            f"  Eligible Modes  : {', '.join(row.eligible_modes) if row.eligible_modes else '—'}"
        )
        lines.append(f"  Created At      : {row.created_at or '—'}")
        lines.append(f"  Claims Production: {eligible}")
        lines.append("")
        if reasons:
            lines.append("Blocking Reasons (cannot production-train):")
            for i, reason in enumerate(reasons, 1):
                lines.append(f"  {i}. {reason}")
        else:
            lines.append("Blocking Reasons: none (production-eligible)")
        return "\n".join(lines)

    # -- filtering -------------------------------------------------------

    def filter_by_readiness(
        self, rows: list[DatasetRegistryRow], level: str
    ) -> list[DatasetRegistryRow]:
        """Return rows whose ``readiness_level`` equals *level*."""
        if level not in READINESS_LEVELS:
            raise ValueError(f"level must be one of {READINESS_LEVELS}, got {level!r}")
        return [row for row in rows if row.readiness_level == level]

    def filter_production_eligible(
        self, rows: list[DatasetRegistryRow]
    ) -> list[DatasetRegistryRow]:
        """Return only rows with readiness >= L3 and quality gate ``passed``."""
        return [
            row
            for row in rows
            if _readiness_rank(row.readiness_level) >= _PRODUCTION_MIN_RANK
            and row.quality_gate_status == "passed"
        ]

    # -- sorting ---------------------------------------------------------

    def sort_rows(self, rows: list[DatasetRegistryRow]) -> list[DatasetRegistryRow]:
        """Return *rows* sorted by ``self.config.sort_by`` / ``sort_order``."""
        reverse = self.config.sort_order == "desc"

        if self.config.sort_by == "dataset_id":
            key = lambda r: r.dataset_id
        elif self.config.sort_by == "readiness_level":
            key = lambda r: _readiness_rank(r.readiness_level)
        elif self.config.sort_by == "created_at":
            key = lambda r: r.created_at
        else:  # pragma: no cover - guarded by config validator
            raise ValueError(f"unknown sort_by: {self.config.sort_by!r}")

        return sorted(rows, key=key, reverse=reverse)
