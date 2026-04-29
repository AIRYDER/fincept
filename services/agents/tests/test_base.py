"""Tests for agents.base.Agent ABC contract."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from pydantic import BaseModel

from agents.base import Agent


class _Tick(BaseModel):
    n: int


class _Concrete(Agent):
    agent_id = "test.concrete"

    def __init__(self) -> None:
        self.setup_called = 0
        self.teardown_called = 0
        self.yields = 3

    async def setup(self) -> None:
        self.setup_called += 1

    async def run(self) -> AsyncIterator[BaseModel]:
        for i in range(self.yields):
            yield _Tick(n=i)

    async def teardown(self) -> None:
        self.teardown_called += 1


def test_agent_cannot_be_instantiated_directly() -> None:
    """Abstract methods enforce a concrete subclass."""
    with pytest.raises(TypeError):
        Agent()  # type: ignore[abstract]


async def test_concrete_agent_lifecycle_runs() -> None:
    agent = _Concrete()
    await agent.setup()
    seen: list[int] = []
    async for event in agent.run():
        assert isinstance(event, _Tick)
        seen.append(event.n)
    await agent.teardown()
    assert seen == [0, 1, 2]
    assert agent.setup_called == 1
    assert agent.teardown_called == 1


def test_agent_id_is_class_level() -> None:
    """The base contract requires agent_id at the class level so it
    embeds in events without per-instance configuration."""
    assert _Concrete.agent_id == "test.concrete"
    assert _Concrete().agent_id == "test.concrete"
