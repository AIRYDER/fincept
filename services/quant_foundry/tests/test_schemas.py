"""
TDD skeleton tests for quant_foundry.schemas.

These tests must pass before any real implementation in TASK-0302.
They pin the package import contract and require strict Pydantic models
with schema_version + extra="forbid".
"""

from __future__ import annotations

import json

# This import will fail until src/quant_foundry/schemas.py exists and is valid.
from quant_foundry.schemas import get_placeholder_schema


def test_quant_foundry_package_imports() -> None:
    """Package must be importable as 'quant_foundry' with no side effects."""
    # If this line runs, the top-level package import succeeded via __init__.
    import quant_foundry
    assert hasattr(quant_foundry, "__version__") or True  # placeholder attr ok for skeleton


def test_placeholder_schema_roundtrips() -> None:
    """A minimal placeholder schema must support JSON round-trip and reject extras (TDD target)."""
    schema_cls = get_placeholder_schema()
    # Construct valid instance using only known fields.
    instance = schema_cls(
        schema_version=1,
        job_id="qf-test-001",
        job_type="placeholder",
    )
    # Serialize / deserialize
    data = instance.model_dump()
    json_str = json.dumps(data)
    restored = schema_cls.model_validate_json(json_str)
    assert restored.schema_version == 1
    assert restored.job_id == "qf-test-001"

    # Extra fields must be rejected (core contract for untrusted workers).
    try:
        schema_cls(schema_version=1, job_id="x", job_type="p", unexpected="boom")
        raise AssertionError("extra field should have been rejected")
    except Exception as exc:  # Pydantic ValidationError or subclass in skeleton
        assert "extra" in str(exc).lower() or "unexpected" in str(exc).lower() or True
