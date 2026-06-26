"""Shared naming validation for identifiers used as filesystem keys.

Several modules (settlement, feature_snapshot, strategy_config,
prediction_log) validate agent_id / strategy_id / decision_id strings
that become directory names or Redis hash keys.  The forbidden
character set and validation rules must be identical across all of
them — extracting to this module ensures the DRY invariant.
"""

from __future__ import annotations

# Characters that are illegal in Windows filenames and/or have special
# meaning in Redis key patterns.  This set is intentionally conservative
# so the same IDs work cross-platform and across all storage backends.
BAD_NAME_CHARS = frozenset('/\\:*?"<>|\0')


def validate_name(name: str, *, field: str = "name") -> None:
    """Validate that *name* is safe to use as a filesystem path component
    or Redis hash key.

    Rules:
      - Must be non-empty.
      - Must not contain any character in ``BAD_NAME_CHARS``.
      - Must not be ``.`` or ``..`` or start with ``.``.

    Raises ``ValueError`` with a descriptive message on violation.
    """
    if not name:
        raise ValueError(f"{field} must be non-empty")
    if any(c in BAD_NAME_CHARS for c in name):
        raise ValueError(f"{field} contains forbidden character: {name!r}")
    if name in {".", ".."} or name.startswith("."):
        raise ValueError(f"{field} may not start with '.': {name!r}")
