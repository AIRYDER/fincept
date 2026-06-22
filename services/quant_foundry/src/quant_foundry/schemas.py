"""
quant_foundry.schemas — stub contracts for Quant Foundry boundary.

Full schemas (QuantFoundryJob, RunPod*Request, ShadowPrediction, etc.) land in TASK-0302.
This file provides the absolute minimum for the skeleton so that:
- Package imports cleanly
- A placeholder model roundtrips JSON
- extra="forbid" is demonstrated immediately (security / contract invariant)

All models here MUST remain frozen + forbid-extras.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class PlaceholderJob(BaseModel):
    """Minimal stand-in used only by the TASK-0301 skeleton tests.

    Real jobs will be defined with authority="shadow-only", schema_version, etc.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    job_id: str
    job_type: str


def get_placeholder_schema() -> type[PlaceholderJob]:
    """Return the placeholder class for TDD import/roundtrip assertions.

    Later replaced by full QuantFoundryJob etc. without changing the test contract.
    """
    return PlaceholderJob
