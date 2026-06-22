"""
quant_foundry.paper_bridge — paper-only model pointer bridge (TASK-0704).

The **first dangerous connection point.** Converts shadow predictions to
paper predictions and publishes them — but only in paper mode, only for
``paper-approved`` models, only when
``QUANT_FOUNDRY_ALLOW_PAPER_BRIDGE=true``.

Key invariants:
- **Bridge is disabled by default.** The config defaults to
  ``allow_paper_bridge=False``.
- **Bridge refuses non-paper runtime.** If ``runtime_mode != "paper"``,
  the bridge refuses to publish.
- **Bridge refuses models without evidence packet.** If no evidence or no
  dossier, the bridge refuses. If the model is not ``paper-approved``, the
  bridge refuses.
- **Rollback pointer exists.** Before publishing, the bridge creates a
  ``RollbackPointer`` so the operator can roll back.
- **Risk/OMS boundaries remain unchanged.** The ``PaperPrediction`` has no
  order/OMS fields — it's a prediction, not an order.
- **Circuit breaker.** If too many failures occur, the circuit breaker
  trips and the bridge refuses all further publishes until reset.

File-disjoint from ``libs/fincept-core/``, ``libs/fincept-bus/``,
``services/orchestrator/``, ``services/risk/``, ``services/oms/`` (other
builders' files). Imports from my ``promotion.py`` (TASK-0702),
``dossier.py`` (TASK-0403), ``schemas.py`` (read-only).
"""

from __future__ import annotations

import os
import time
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict

from quant_foundry.dossier import DossierStatus
from quant_foundry.promotion import PromotionEvidence

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class BridgeConfig(BaseModel):
    """Configuration for the paper bridge.

    Frozen + extra='forbid'. ``allow_paper_bridge`` defaults to False
    (disabled by default). ``runtime_mode`` defaults to "paper".
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    allow_paper_bridge: bool = False
    runtime_mode: str = "paper"


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


class BridgeStatus(StrEnum):
    """The status of a bridge publish attempt."""

    ENABLED = "enabled"
    DISABLED = "disabled"
    PUBLISHED = "published"
    REFUSED = "refused"


# ---------------------------------------------------------------------------
# Paper prediction (converted from shadow)
# ---------------------------------------------------------------------------


class PaperPrediction(BaseModel):
    """A shadow prediction converted to the paper prediction schema.

    Frozen + extra='forbid'. Carries prediction fields only — no
    order/OMS/risk fields. Risk and OMS remain authoritative.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    prediction_id: str
    model_id: str
    symbol: str
    ts_event: int
    horizon_ns: int
    direction: float
    confidence: float
    p_up: float
    authority: str = "paper-only"


# ---------------------------------------------------------------------------
# Rollback pointer
# ---------------------------------------------------------------------------


class RollbackPointer(BaseModel):
    """A rollback pointer created before publishing.

    Frozen + extra='forbid'. Carries the model_id, pointer_id, timestamp,
    and reason. The operator can use this to roll back the bridge.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    model_id: str
    pointer_id: str
    created_at_ns: int
    reason: str


# ---------------------------------------------------------------------------
# Receipt
# ---------------------------------------------------------------------------


class BridgeReceipt(BaseModel):
    """Receipt for a bridge publish attempt.

    Frozen + extra='forbid'. Carries the status, reason, converted
    prediction (if published), and rollback pointer (if created).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: BridgeStatus
    reason: str = ""
    prediction: PaperPrediction | None = None
    rollback_pointer: RollbackPointer | None = None
    published_at_ns: int = 0

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable dict for audit/persistence."""
        return {
            "status": self.status.value,
            "reason": self.reason,
            "prediction": self.prediction.model_dump() if self.prediction else None,
            "rollback_pointer": (
                self.rollback_pointer.model_dump()
                if self.rollback_pointer
                else None
            ),
            "published_at_ns": self.published_at_ns,
        }


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------


class BridgeCircuitBreaker:
    """Circuit breaker for the paper bridge.

    Trips after ``failure_threshold`` failures. Once tripped, the bridge
    refuses all further publishes until reset.
    """

    def __init__(self, failure_threshold: int = 5) -> None:
        self.failure_threshold = failure_threshold
        self._failure_count = 0
        self._tripped = False

    def record_failure(self) -> None:
        """Record a failure. Trips the breaker if threshold is reached."""
        self._failure_count += 1
        if self._failure_count >= self.failure_threshold:
            self._tripped = True

    def record_success(self) -> None:
        """Record a success. Resets the failure count."""
        self._failure_count = 0

    def is_tripped(self) -> bool:
        """Return True if the circuit breaker is tripped."""
        return self._tripped

    def reset(self) -> None:
        """Reset the circuit breaker."""
        self._failure_count = 0
        self._tripped = False


# ---------------------------------------------------------------------------
# The bridge
# ===========================================================================


class PaperBridge:
    """The paper-only model pointer bridge.

    Converts shadow predictions to paper predictions and publishes them,
    subject to strict guards:
    1. Bridge must be enabled (``allow_paper_bridge=True``).
    2. Runtime must be paper (``runtime_mode="paper"``).
    3. Evidence packet must be present with a ``paper-approved`` dossier.
    4. Circuit breaker must not be tripped.

    If all guards pass, the bridge creates a rollback pointer, converts
    the shadow prediction to a ``PaperPrediction``, and returns a
    ``BridgeReceipt`` with ``PUBLISHED`` status.
    """

    def __init__(
        self,
        config: BridgeConfig | None = None,
        circuit_breaker: BridgeCircuitBreaker | None = None,
    ) -> None:
        # Check the env var if no config provided.
        if config is None:
            env_val = os.environ.get("QUANT_FOUNDRY_ALLOW_PAPER_BRIDGE", "").lower()
            allow = env_val == "true"
            config = BridgeConfig(allow_paper_bridge=allow)
        self.config = config
        self.circuit_breaker = circuit_breaker or BridgeCircuitBreaker()

    @property
    def status(self) -> BridgeStatus:
        """Return the bridge's current status."""
        if not self.config.allow_paper_bridge:
            return BridgeStatus.DISABLED
        return BridgeStatus.ENABLED

    def publish(
        self,
        prediction: dict[str, Any],
        evidence: PromotionEvidence | None,
    ) -> BridgeReceipt:
        """Publish a shadow prediction as a paper prediction.

        Returns a ``BridgeReceipt`` with the status, reason, converted
        prediction (if published), and rollback pointer (if created).
        """
        now_ns = time.time_ns()

        # 1. Bridge must be enabled.
        if not self.config.allow_paper_bridge:
            return BridgeReceipt(
                status=BridgeStatus.REFUSED,
                reason="bridge is disabled (QUANT_FOUNDRY_ALLOW_PAPER_BRIDGE != true)",
                published_at_ns=now_ns,
            )

        # 2. Circuit breaker must not be tripped.
        if self.circuit_breaker.is_tripped():
            return BridgeReceipt(
                status=BridgeStatus.REFUSED,
                reason="circuit breaker tripped (too many failures)",
                published_at_ns=now_ns,
            )

        # 3. Runtime must be paper.
        if self.config.runtime_mode != "paper":
            return BridgeReceipt(
                status=BridgeStatus.REFUSED,
                reason=f"non-paper runtime: {self.config.runtime_mode}",
                published_at_ns=now_ns,
            )

        # 4. Evidence packet must be present.
        if evidence is None:
            self.circuit_breaker.record_failure()
            return BridgeReceipt(
                status=BridgeStatus.REFUSED,
                reason="missing evidence packet",
                published_at_ns=now_ns,
            )

        # 5. Dossier must be present.
        if evidence.dossier is None:
            self.circuit_breaker.record_failure()
            return BridgeReceipt(
                status=BridgeStatus.REFUSED,
                reason="missing dossier in evidence packet",
                published_at_ns=now_ns,
            )

        # 6. Model must be paper-approved.
        if evidence.dossier.status != DossierStatus.PAPER_APPROVED:
            self.circuit_breaker.record_failure()
            return BridgeReceipt(
                status=BridgeStatus.REFUSED,
                reason=(
                    f"model not paper-approved "
                    f"(status={evidence.dossier.status.value})"
                ),
                published_at_ns=now_ns,
            )

        # 7. Create rollback pointer.
        model_id = prediction.get("model_id", "")
        rollback_pointer = RollbackPointer(
            model_id=model_id,
            pointer_id=f"rb-{prediction.get('prediction_id', '')}",
            created_at_ns=now_ns,
            reason="paper bridge publish",
        )

        # 8. Convert shadow prediction to paper prediction.
        paper_pred = convert_shadow_to_paper(prediction)

        # 9. Publish (record success).
        self.circuit_breaker.record_success()

        return BridgeReceipt(
            status=BridgeStatus.PUBLISHED,
            reason="published to paper stream",
            prediction=paper_pred,
            rollback_pointer=rollback_pointer,
            published_at_ns=now_ns,
        )


# ---------------------------------------------------------------------------
# Convenience function
# ---------------------------------------------------------------------------


def convert_shadow_to_paper(prediction: dict[str, Any]) -> PaperPrediction:
    """Convert a shadow prediction dict to a PaperPrediction.

    Carries prediction fields only — no order/OMS/risk fields.
    """
    return PaperPrediction(
        prediction_id=str(prediction["prediction_id"]),
        model_id=str(prediction["model_id"]),
        symbol=str(prediction["symbol"]),
        ts_event=int(prediction["ts_event"]),
        horizon_ns=int(prediction["horizon_ns"]),
        direction=float(prediction["direction"]),
        confidence=float(prediction["confidence"]),
        p_up=float(prediction.get("p_up", 0.5)),
    )
