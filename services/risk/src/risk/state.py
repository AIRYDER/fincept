"""
risk.state - in-memory KillSwitchState.

The kill-switch is engaged or cleared by ``AlertEvent`` records on
``STREAM_ALERTS``.  Canonical codes (set by ``services/api`` in
:mod:`api.routes.control`):

  - ``code="kill_switch_engaged"``  severity="critical"  -> flip engaged=True
  - ``code="kill_switch_cleared"``  severity="info"      -> flip engaged=False

The engaged flag is consulted by ``check_intent`` on every order intent.
When engaged, *every* intent is rejected regardless of notional, until
an operator publishes the cleared alert.

State is in-memory (no persistence).  On OMS restart the flag resets to
False; the alert consumer's ``$``-cursor live tail will not see prior
alerts.  This is acceptable for v1 because:

  1. Engaging the kill switch is an explicit operator action; if the
     OMS restarts during an outage, the operator should re-publish.
  2. The alternative - persisting to Redis - introduces a write path
     that could fail and leave the system in a stale-engaged state.

A persistent kill-switch flag is a Phase H concern (TASK-070 chaos
suite).
"""

from __future__ import annotations

from fincept_core.logging import get_logger
from fincept_core.schemas import AlertEvent

log = get_logger(__name__)

CODE_ENGAGED = "kill_switch_engaged"
CODE_CLEARED = "kill_switch_cleared"


class KillSwitchState:
    """In-memory boolean flag fed by AlertEvents.

    Thread-safety: not thread-safe.  Asyncio-safe because all updates
    come from a single ``apply`` call inside the alert consumer task.
    """

    def __init__(self) -> None:
        self._engaged = False

    @property
    def engaged(self) -> bool:
        return self._engaged

    def apply(self, event: AlertEvent) -> None:
        """Apply an alert.  Non-kill-switch alerts are ignored."""
        if event.code == CODE_ENGAGED:
            if not self._engaged:
                log.warning(
                    "risk.kill_switch.engaged",
                    alert_id=event.alert_id,
                    severity=event.severity,
                    source=event.source,
                    message=event.message,
                )
            self._engaged = True
        elif event.code == CODE_CLEARED:
            if self._engaged:
                log.info(
                    "risk.kill_switch.cleared",
                    alert_id=event.alert_id,
                    source=event.source,
                    message=event.message,
                )
            self._engaged = False
