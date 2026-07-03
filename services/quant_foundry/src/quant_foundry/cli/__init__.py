"""
quant_foundry.cli — unified RunPod CLI surface (T-OP.1).

This package provides a single safe command surface that replaces the
scattered entry points into the RunPod training pipeline. No command
trains locally — local execution is limited to manifest validation,
request construction, and receipt verification.

Public surface:
  - :class:`CLIConfig` — frozen Pydantic v2 config for the CLI.
  - :class:`CommandSpec` — description of one CLI command.
  - :class:`CommandResult` — result of one CLI command invocation.
  - :class:`RunPodCLI` — the unified dispatcher.
  - :func:`validate_preflight` — fail-closed preflight checks.
  - :func:`list_commands` — enumerate available commands.
  - :func:`render_help` — formatted help text.
  - :func:`format_status_report` — readable job status report.
"""

from __future__ import annotations

from quant_foundry.cli.runpod_cli import (
    CLIConfig,
    CommandResult,
    CommandSpec,
    RunPodCLI,
    format_status_report,
    list_commands,
    render_help,
    validate_preflight,
)

__all__ = [
    "CLIConfig",
    "CommandResult",
    "CommandSpec",
    "RunPodCLI",
    "format_status_report",
    "list_commands",
    "render_help",
    "validate_preflight",
]
