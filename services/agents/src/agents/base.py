"""
agents.base - abstract Agent contract.

Concrete agents subclass ``Agent`` and implement three lifecycle hooks:

  - :meth:`setup`     One-time initialisation (load model, warm caches,
                       open connections).  Called by the entrypoint
                       once before the run loop.
  - :meth:`run`       Async generator yielding events (typically
                       ``Prediction`` instances).  The entrypoint
                       publishes each yielded event to the bus.
  - :meth:`teardown`  Symmetric cleanup of :meth:`setup`.  Idempotent.

The contract is intentionally minimal: each agent has its OWN policy
for cadence, batch shape, exception handling, and reconnect logic.
The base class only owns the lifecycle shape so a single
``services/agents/src/agents/<name>/main.py`` template can host any
implementation.

``agent_id`` is class-level (e.g. ``"gbm_predictor.v1"``) so it can be
embedded in every emitted event without per-instance configuration.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

from pydantic import BaseModel


class Agent(ABC):
    """Abstract base for all v1 strategy agents."""

    #: Stable identifier for this agent revision.  Embedded in every
    #: emitted ``Prediction`` so consumers (orchestrator, audit log,
    #: blotter) can attribute predictions to a specific revision.
    agent_id: str

    @abstractmethod
    async def setup(self) -> None:
        """One-time initialisation; called once before :meth:`run`."""

    @abstractmethod
    def run(self) -> AsyncIterator[BaseModel]:
        """Yield events forever (or until cancelled).

        Implementations should respect cooperative cancellation via
        ``asyncio.CancelledError`` raised at every ``await``.
        """

    @abstractmethod
    async def teardown(self) -> None:
        """Cleanup symmetric to :meth:`setup`.  Idempotent."""
