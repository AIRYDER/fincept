"""Tests for risk.state.KillSwitchState."""

from __future__ import annotations

from fincept_core.schemas import AlertEvent
from risk.state import KillSwitchState


def _alert(*, code: str, severity: str = "critical") -> AlertEvent:
    return AlertEvent(
        alert_id="a1",
        ts_event=1_000,
        severity=severity,
        source="api.control",
        code=code,
        message=f"test {code}",
    )


def test_kill_switch_starts_disengaged() -> None:
    state = KillSwitchState()
    assert state.engaged is False


def test_engaged_alert_flips_flag() -> None:
    state = KillSwitchState()
    state.apply(_alert(code="kill_switch_engaged"))
    assert state.engaged is True


def test_cleared_alert_after_engaged_resets_flag() -> None:
    state = KillSwitchState()
    state.apply(_alert(code="kill_switch_engaged"))
    assert state.engaged is True
    state.apply(_alert(code="kill_switch_cleared", severity="info"))
    assert state.engaged is False


def test_unknown_code_does_not_change_flag() -> None:
    state = KillSwitchState()
    state.apply(_alert(code="some_other_alert"))
    assert state.engaged is False

    state.apply(_alert(code="kill_switch_engaged"))
    state.apply(_alert(code="another_unrelated_alert"))
    assert state.engaged is True


def test_repeated_engaged_alerts_remain_engaged() -> None:
    state = KillSwitchState()
    state.apply(_alert(code="kill_switch_engaged"))
    state.apply(_alert(code="kill_switch_engaged"))
    assert state.engaged is True


def test_cleared_alert_when_already_disengaged_is_idempotent() -> None:
    state = KillSwitchState()
    state.apply(_alert(code="kill_switch_cleared", severity="info"))
    assert state.engaged is False
