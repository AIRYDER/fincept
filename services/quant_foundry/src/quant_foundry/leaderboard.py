"""
quant_foundry.leaderboard — ranked leaderboard of tournament results (TASK-0404).

The leaderboard ranks models by their tournament total score. A model with
``INSUFFICIENT_EVIDENCE`` or ``STALE`` status is never ranked above a model
with sufficient, fresh evidence — even if its (pre-gate) score is higher
(insufficient-evidence models have score 0.0 by construction, but the
leaderboard also enforces the ordering explicitly so the invariant is
robust to future changes in the scorer).

The leaderboard is in-memory for the MVP skeleton (the dossier registry
handles durable storage; the leaderboard is a transient view produced on
demand). ``to_dict`` is JSON serializable so the leaderboard can feed a
promotion packet.

File-disjoint from all active builders (see BUILDER3.md).
"""

from __future__ import annotations

from typing import Any

# Re-export PromotionRecommendation so callers can import everything from
# the leaderboard module without reaching into tournament.py.
from quant_foundry.tournament import (
    TournamentResult,
    TournamentStatus,
)

# Status rank priority — lower number = ranked higher when scores tie, AND
# insufficient/stale models are pushed to the bottom regardless of score.
_STATUS_PRIORITY: dict[TournamentStatus, int] = {
    TournamentStatus.ELIGIBLE: 0,
    TournamentStatus.BLOCKED: 1,
    TournamentStatus.STALE: 2,
    TournamentStatus.INSUFFICIENT_EVIDENCE: 3,
}


class Leaderboard:
    """In-memory ranked leaderboard of tournament results.

    Models are sorted by:
    1. status priority (ELIGIBLE > BLOCKED > STALE > INSUFFICIENT_EVIDENCE)
       — a model with insufficient evidence is never ranked above a model
       with sufficient evidence, regardless of score.
    2. total_score (descending) within the same status priority.

    The leaderboard is a transient view; durability is the dossier
    registry's job. ``to_dict`` produces a JSON-serializable promotion
    packet.
    """

    def __init__(self) -> None:
        self._results: list[TournamentResult] = []

    def add(self, result: TournamentResult) -> None:
        """Add a tournament result to the leaderboard."""
        self._results.append(result)

    def ranked(self) -> list[TournamentResult]:
        """Return the results ranked best-first.

        ELIGIBLE models come first (sorted by score descending), then
        BLOCKED, then STALE, then INSUFFICIENT_EVIDENCE. Within a status
        group, higher total_score ranks higher.
        """
        return sorted(
            self._results,
            key=lambda r: (
                _STATUS_PRIORITY.get(r.status, 99),
                -r.total_score,
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable dict for promotion-packet emission."""
        return {
            "ranked": [r.to_dict() for r in self.ranked()],
        }
