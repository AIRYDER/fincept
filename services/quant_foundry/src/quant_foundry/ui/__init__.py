"""UI layer for the Quant Foundry dataset registry.

Pure text/markdown rendering — no external UI dependencies.
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

__all__ = [
    "DatasetRegistryRow",
    "DatasetRegistryView",
    "DatasetRegistryViewConfig",
    "format_quality_gate",
    "format_readiness",
    "format_upload_status",
    "get_blocking_reasons",
    "validate_no_false_readiness",
]
