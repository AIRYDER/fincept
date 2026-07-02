"""UI layer for the Quant Foundry.

Pure text/markdown rendering — no external UI dependencies. Each view module
is file-disjoint; this package re-exports the public surface of every view
so callers can import from ``quant_foundry.ui`` directly.
"""

from quant_foundry.ui.dataset_registry_view import (
    DatasetRegistryRow,
    DatasetRegistryView,
    DatasetRegistryViewConfig,
    format_quality_gate,
    format_readiness,
    format_upload_status,
    get_blocking_reasons,
    validate_no_false_readiness,
)
from quant_foundry.ui.model_tournament_view import (
    TournamentRow,
    TournamentView,
    TournamentViewConfig,
    find_best_in_column,
    format_delta,
    format_eligibility,
    format_metric,
    validate_no_inflated_confidence,
)

__all__ = [
    "DatasetRegistryRow",
    "DatasetRegistryView",
    "DatasetRegistryViewConfig",
    "format_quality_gate",
    "format_readiness",
    "format_upload_status",
    "get_blocking_reasons",
    "validate_no_false_readiness",
    "TournamentRow",
    "TournamentView",
    "TournamentViewConfig",
    "find_best_in_column",
    "format_delta",
    "format_eligibility",
    "format_metric",
    "validate_no_inflated_confidence",
]
