"""Label computer modules. Importing this package registers all label modules."""

from __future__ import annotations

from quant_foundry.modules.labels.abnormal_return import (
    AbnormalReturnLabel,
    AbnormalReturnLabelV1,
)

__all__ = ["AbnormalReturnLabel", "AbnormalReturnLabelV1"]
