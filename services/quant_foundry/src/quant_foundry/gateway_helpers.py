"""quant_foundry.gateway_helpers — pure helper functions extracted from gateway.py.

These functions have no dependency on the QuantFoundryGateway class state
and are extracted to reduce gateway.py's size. They fall into three groups:

1. Alpha Genome helpers — adapter, mock outcome, default dispatcher/probe,
   sweep receipt serialization.
2. Shadow health aggregation — percentile, feature availability fraction.
3. RunPod/feature parsing — mode checks, job type normalization, endpoint
   ID extraction, callback field extraction, feature row deserialization.
"""

from __future__ import annotations

import dataclasses
from typing import Any

from quant_foundry.feature_lake import FeatureRow, FeatureValue

# ---------------------------------------------------------------------------
# Alpha Genome helpers
# ---------------------------------------------------------------------------


class AlphaDossierUpsertAdapter:
    """Adapter that exposes ``upsert(dossier)`` on a ``DossierRegistry``.

    ``AlphaGenomeLab.run_sweep`` calls ``registry.upsert(dossier)`` to
    register a candidate recipe's dossier. The canonical registry exposes
    ``register(...)`` (idempotent, security-checked). This adapter bridges
    the two names without forcing the lab to know about registry internals.
    """

    def __init__(self, registry: Any) -> None:
        self._registry = registry

    def upsert(self, dossier: Any) -> Any:
        """Forward to ``DossierRegistry.register``."""
        return self._registry.register(dossier)


@dataclasses.dataclass
class AlphaMockTrainingOutcome:
    """Mock TrainingOutcome used by the default dispatcher.

    Carries the minimum fields ``AlphaGenomeLab.run_sweep`` reads off the
    outcome (``model_id``, ``cost_cents``, ``dossier_evidence``). The
    dossier_evidence is intentionally None so the gate rejects with
    ``NO_DOSSIER`` — the safe default path. A real dispatcher must supply
    a real ``DossierRecord``.
    """

    model_id: str
    cost_cents: int = 0
    duration_seconds: float = 0.0
    dossier_evidence: Any = None
    tournament_result: Any = None
    sentinel_receipt: Any = None


def alpha_default_dispatcher(recipe: Any) -> Any:
    """Default dispatcher for the Alpha Genome Lab.

    Returns a benign TrainingOutcome so the sweep can be observed
    end-to-end without GPU spend. The lab's gate rejects with
    ``NO_DOSSIER`` because ``dossier_evidence`` is None — this is the
    safe path. Operators wire a real dispatcher (RunPod or local training)
    in production.
    """
    return AlphaMockTrainingOutcome(
        model_id=f"alpha-mock-{recipe.recipe_id}",
        cost_cents=0,
        duration_seconds=0.0,
        dossier_evidence=None,
        tournament_result=None,
        sentinel_receipt=None,
    )


def alpha_default_tournament_probe(recipe_id: str) -> None:
    """Default tournament probe — always returns None (no early stop)."""
    return None


def sweep_receipt_to_dict(receipt: Any) -> dict[str, Any]:
    """Convert a SweepReceipt dataclass to a JSON-safe dict.

    The trial_receipts list is converted to a list of dicts; the rest of
    the top-level fields are scalars. No secrets — the SweepReceipt
    carries only recipe ids, hashes, and counts.
    """
    trials: list[dict[str, Any]] = []
    for tr in receipt.trial_receipts:
        trials.append(
            {
                "recipe_id": tr.recipe_id,
                "parent_recipe_id": tr.parent_recipe_id,
                "status": tr.status.value,
                "reason": tr.reason,
                "model_id": tr.model_id,
                "cost_cents": tr.cost_cents,
                "duration_seconds": tr.duration_seconds,
                "promotion_decision": tr.promotion_decision,
                "sweep_id": tr.sweep_id,
            }
        )
    return {
        "sweep_id": receipt.sweep_id,
        "seed_recipe_id": receipt.seed_recipe_id,
        "n_recipes": receipt.n_recipes,
        "n_registered": receipt.n_registered,
        "n_rejected": receipt.n_rejected,
        "n_killed_early": receipt.n_killed_early,
        "n_discarded": receipt.n_discarded,
        "sweep_cost_cents": receipt.sweep_cost_cents,
        "started_at_ns": receipt.started_at_ns,
        "ended_at_ns": receipt.ended_at_ns,
        "trial_receipts": trials,
    }


# ---------------------------------------------------------------------------
# Shadow health aggregation helpers
# ---------------------------------------------------------------------------


def percentile(sorted_values: list[float], pct: float) -> float:
    """Linear-interpolation percentile over an already-sorted numeric list."""
    if not sorted_values:
        raise ValueError("percentile of empty sequence")
    if pct <= 0:
        return sorted_values[0]
    if pct >= 1:
        return sorted_values[-1]
    position = pct * (len(sorted_values) - 1)
    lower = int(position)
    upper = min(lower + 1, len(sorted_values) - 1)
    weight = position - lower
    return sorted_values[lower] * (1 - weight) + sorted_values[upper] * weight


def aggregate_feature_availability(records: list[Any]) -> float | None:
    """Fraction of features marked available across all stored predictions.

    Returns ``None`` when no record carries a ``feature_availability`` map —
    preserves the spec's "null for uncomputable" contract.
    """
    available = 0
    total = 0
    for r in records:
        fa = getattr(r, "feature_availability", None)
        if not fa:
            continue
        for present in fa.values():
            total += 1
            if present:
                available += 1
    if total == 0:
        return None
    return available / total


# ---------------------------------------------------------------------------
# RunPod / feature parsing helpers
# ---------------------------------------------------------------------------


def is_runpod_mode_value(mode: str) -> bool:
    return mode in {"runpod", "runpod_research", "runpod_shadow"}


# --- env var resolution (canonical + deprecated fallback) ------------------


def env_first(primary: str, *fallbacks: str, default: str = "") -> str:
    """Resolve an env var preferring the canonical name, falling back to
    deprecated names with a warning.

    Canonical names win if both are present. Deprecated fallbacks emit a
    ``DeprecationWarning`` so operators can migrate without breakage, but
    the migration path is visible in logs.

    Args:
        primary: the canonical env var name (read first).
        *fallbacks: deprecated env var names, tried in order.
        default: returned when neither primary nor any fallback is set.
    """
    import os
    import warnings

    value = os.environ.get(primary)
    if value:
        return value

    for fallback in fallbacks:
        value = os.environ.get(fallback)
        if value:
            warnings.warn(
                f"Using deprecated env var {fallback!r}; please migrate to {primary!r}.",
                DeprecationWarning,
                stacklevel=2,
            )
            return value

    return default


def normalize_job_type(job_type: str) -> str:
    return str(job_type).lower()


def client_endpoint_id(client: Any) -> str | None:
    endpoint_id = getattr(client, "endpoint_id", None)
    if endpoint_id is None:
        endpoint_id = getattr(client, "_endpoint_id", None)
    if endpoint_id is None:
        return None
    return str(endpoint_id)


def runpod_status_value(status: dict[str, Any]) -> str:
    value = status.get("status") or status.get("state") or status.get("runtimeStatus")
    if value is None:
        return "UNKNOWN"
    return str(value).upper()


def extract_callback_fields(output: Any) -> tuple[str, str, int] | None:
    if not isinstance(output, dict):
        return None
    nested_output = output.get("output")
    if isinstance(nested_output, dict):
        nested_fields = extract_callback_fields(nested_output)
        if nested_fields is not None:
            return nested_fields

    payload = output.get("callback_payload")
    signature = output.get("callback_signature")
    ts = output.get("callback_ts")
    if not isinstance(payload, str) or not isinstance(signature, str):
        return None
    if not isinstance(ts, (int, str)):
        return None
    try:
        callback_ts = int(ts)
    except (TypeError, ValueError):
        return None
    return payload, signature, callback_ts


def decision_time_from_payload(
    request_payload: dict[str, Any],
    rows_payload: Any,
) -> int:
    raw_decision_time = request_payload.get("decision_time")
    if raw_decision_time is not None:
        return int(raw_decision_time)
    if not isinstance(rows_payload, (list, tuple)) or not rows_payload:
        raise ValueError("feature_rows must be a non-empty list when decision_time is omitted")
    first_row = rows_payload[0]
    if isinstance(first_row, FeatureRow):
        return int(first_row.decision_time)
    if isinstance(first_row, dict) and "decision_time" in first_row:
        return int(first_row["decision_time"])
    raise ValueError("decision_time is required when feature_rows lack decision_time")


def feature_row_from_payload(row: Any) -> FeatureRow:
    if isinstance(row, FeatureRow):
        return row
    if not isinstance(row, dict):
        raise TypeError("feature_rows entries must be objects")
    features_payload = row.get("features")
    if not isinstance(features_payload, (list, tuple)):
        raise TypeError("feature_rows[].features must be a list")
    features = tuple(
        feature if isinstance(feature, FeatureValue) else FeatureValue(**feature)
        for feature in features_payload
    )
    return FeatureRow(
        symbol=str(row["symbol"]),
        event_ts=int(row["event_ts"]),
        decision_time=int(row["decision_time"]),
        features=features,
        label_horizon_ns=int(row.get("label_horizon_ns", 86_400_000_000_000)),
    )
