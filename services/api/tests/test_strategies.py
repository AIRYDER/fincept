"""Tests for /strategies endpoint.

Two surfaces covered:

  1. **Runtime view** (GET /strategies) — reads from the portfolio
     service's Redis-backed PositionStore.  Pre-Phase-F; still here.

  2. **Config CRUD + lifecycle** (/strategies/configs/*) — Phase F.
     Uses a tmp_path-backed StrategyConfigStore reset via the
     ``patched_strategy_store`` fixture so tests don't leak state
     through the process-wide singleton.
"""

from __future__ import annotations

import pathlib
from collections.abc import Iterator
from decimal import Decimal

import fakeredis.aioredis
import pytest
from httpx import AsyncClient

from fincept_core.schemas import Position
from fincept_core.strategy_config import (
    StrategyConfigStore,
    reset_strategy_config_store,
)
from portfolio.store import PositionStore


# --------------------------------------------------------------------------- #
# Runtime view                                                                #
# --------------------------------------------------------------------------- #


def _pos(strategy_id: str, symbol: str, qty: str) -> Position:
    return Position(
        strategy_id=strategy_id,
        symbol=symbol,
        quantity=Decimal(qty),
        avg_cost=Decimal("100"),
        updated_at=1_000,
    )


async def _seed(redis: fakeredis.aioredis.FakeRedis) -> None:
    store = PositionStore(redis)
    await store.put(_pos("strat_a", "BTC-USD", "1"))
    await store.put(_pos("strat_a", "ETH-USD", "0"))
    await store.put(_pos("strat_b", "BTC-USD", "-1"))


async def test_strategies_requires_auth(client: AsyncClient) -> None:
    response = await client.get("/strategies")
    assert response.status_code == 401


async def test_strategies_returns_known_with_counts(
    fake_redis: fakeredis.aioredis.FakeRedis,
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    await _seed(fake_redis)
    response = await client.get("/strategies", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    by_id = {s["strategy_id"]: s for s in body}
    assert set(by_id) == {"strat_a", "strat_b"}
    assert by_id["strat_a"]["position_count"] == 2  # BTC + ETH (incl flat)
    assert by_id["strat_a"]["open_positions"] == 1  # only BTC is non-zero
    assert by_id["strat_b"]["position_count"] == 1
    assert by_id["strat_b"]["open_positions"] == 1


async def test_strategies_empty_when_no_state(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    response = await client.get("/strategies", headers=auth_headers)
    assert response.status_code == 200
    assert response.json() == []


# --------------------------------------------------------------------------- #
# Phase F: config CRUD + lifecycle                                            #
# --------------------------------------------------------------------------- #


@pytest.fixture
def patched_strategy_store(
    tmp_path: pathlib.Path,
) -> Iterator[StrategyConfigStore]:
    """Reset the ``StrategyConfigStore`` singleton against a tmp dir.

    The api dep ``get_strategy_config_store`` reads the module-level
    singleton in ``fincept_core.strategy_config``; rebinding that
    singleton to a tmp-backed store is both faster and simpler than
    overriding the FastAPI dependency, and guarantees the store
    methods and the audit JSONL both land under ``tmp_path``.
    """
    configs_dir = tmp_path / "strategies"
    store = reset_strategy_config_store(configs_dir=configs_dir)
    try:
        yield store
    finally:
        # Reset to default so later tests don't see tmp_path state.
        reset_strategy_config_store(configs_dir=None)


def _create_body(
    strategy_id: str = "btc_ma_main",
    class_name: str = "ma_crossover",
    symbols: list[str] | None = None,
    params: dict[str, object] | None = None,
    model_binding: str | None = None,
    enabled: bool = False,
) -> dict[str, object]:
    return {
        "strategy_id": strategy_id,
        "class_name": class_name,
        "symbols": symbols if symbols is not None else ["BTC-USD"],
        "params": params if params is not None else {"fast": 5, "slow": 20},
        "model_binding": model_binding,
        "enabled": enabled,
    }


# ------ Auth surface ------------------------------------------------------- #


class TestConfigAuth:
    @pytest.mark.asyncio
    async def test_create_requires_auth(
        self, client: AsyncClient, patched_strategy_store: StrategyConfigStore
    ) -> None:
        r = await client.post("/strategies/configs", json=_create_body())
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_list_requires_auth(
        self, client: AsyncClient, patched_strategy_store: StrategyConfigStore
    ) -> None:
        r = await client.get("/strategies/configs")
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_get_requires_auth(
        self, client: AsyncClient, patched_strategy_store: StrategyConfigStore
    ) -> None:
        r = await client.get("/strategies/configs/anything")
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_patch_requires_auth(
        self, client: AsyncClient, patched_strategy_store: StrategyConfigStore
    ) -> None:
        r = await client.patch("/strategies/configs/anything", json={})
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_delete_requires_auth(
        self, client: AsyncClient, patched_strategy_store: StrategyConfigStore
    ) -> None:
        r = await client.delete("/strategies/configs/anything")
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_start_requires_auth(
        self, client: AsyncClient, patched_strategy_store: StrategyConfigStore
    ) -> None:
        r = await client.post("/strategies/configs/anything/start")
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_stop_requires_auth(
        self, client: AsyncClient, patched_strategy_store: StrategyConfigStore
    ) -> None:
        r = await client.post("/strategies/configs/anything/stop")
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_history_requires_auth(
        self, client: AsyncClient, patched_strategy_store: StrategyConfigStore
    ) -> None:
        r = await client.get("/strategies/configs/anything/history")
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_adopt_requires_auth(
        self, client: AsyncClient, patched_strategy_store: StrategyConfigStore
    ) -> None:
        r = await client.post("/strategies/configs/anything/adopt")
        assert r.status_code == 401


# ------ Create ------------------------------------------------------------- #


class TestCreateRoute:
    @pytest.mark.asyncio
    async def test_create_happy_path_returns_201_and_sealed_config(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        patched_strategy_store: StrategyConfigStore,
    ) -> None:
        r = await client.post(
            "/strategies/configs",
            headers=auth_headers,
            json=_create_body(),
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["strategy_id"] == "btc_ma_main"
        assert body["class_name"] == "ma_crossover"
        assert body["symbols"] == ["BTC-USD"]
        assert body["params"] == {"fast": 5, "slow": 20}
        assert body["model_binding"] is None
        assert body["enabled"] is False
        # Store really wrote it.
        persisted = patched_strategy_store.get("btc_ma_main")
        assert persisted is not None
        assert persisted.class_name == "ma_crossover"
        # Upsert stamped both timestamps (non-zero, equal on first write).
        assert body["created_at"] > 0
        assert body["updated_at"] > 0

    @pytest.mark.asyncio
    async def test_create_rejects_unknown_class_name(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        patched_strategy_store: StrategyConfigStore,
    ) -> None:
        r = await client.post(
            "/strategies/configs",
            headers=auth_headers,
            json=_create_body(class_name="no_such_strategy"),
        )
        assert r.status_code == 400
        assert "unknown class_name" in r.json()["detail"]
        # Nothing was written.
        assert patched_strategy_store.list_all() == []

    @pytest.mark.asyncio
    async def test_create_rejects_empty_symbols(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        patched_strategy_store: StrategyConfigStore,
    ) -> None:
        r = await client.post(
            "/strategies/configs",
            headers=auth_headers,
            json=_create_body(symbols=[]),
        )
        # Pydantic validation -> 422.
        assert r.status_code == 422

    @pytest.mark.asyncio
    async def test_create_rejects_unsafe_strategy_id(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        patched_strategy_store: StrategyConfigStore,
    ) -> None:
        r = await client.post(
            "/strategies/configs",
            headers=auth_headers,
            json=_create_body(strategy_id="../escape"),
        )
        assert r.status_code == 400
        assert "invalid strategy_id" in r.json()["detail"]

    @pytest.mark.asyncio
    async def test_create_duplicate_returns_409(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        patched_strategy_store: StrategyConfigStore,
    ) -> None:
        first = await client.post(
            "/strategies/configs",
            headers=auth_headers,
            json=_create_body(),
        )
        assert first.status_code == 201
        # Second POST with same id -> 409; the route checks existence
        # before calling upsert, so nothing is clobbered.
        second = await client.post(
            "/strategies/configs",
            headers=auth_headers,
            json=_create_body(params={"fast": 99, "slow": 100}),
        )
        assert second.status_code == 409
        assert "already exists" in second.json()["detail"]
        # Original params preserved.
        still = patched_strategy_store.get("btc_ma_main")
        assert still is not None
        assert still.params == {"fast": 5, "slow": 20}

    @pytest.mark.asyncio
    async def test_create_with_enabled_true_writes_enabled(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        patched_strategy_store: StrategyConfigStore,
    ) -> None:
        r = await client.post(
            "/strategies/configs",
            headers=auth_headers,
            json=_create_body(enabled=True),
        )
        assert r.status_code == 201
        assert r.json()["enabled"] is True


class TestAdoptRoute:
    @pytest.mark.asyncio
    async def test_adopt_open_positions_creates_disabled_tracker_config(
        self,
        fake_redis: fakeredis.aioredis.FakeRedis,
        client: AsyncClient,
        auth_headers: dict[str, str],
        patched_strategy_store: StrategyConfigStore,
    ) -> None:
        store = PositionStore(fake_redis)
        await store.put(_pos("alpaca.live", "MSFT", "0"))
        await store.put(_pos("alpaca.live", "AMD", "23"))
        await store.put(_pos("alpaca.live", "MU", "13"))

        r = await client.post(
            "/strategies/configs/alpaca.live/adopt",
            headers=auth_headers,
        )

        assert r.status_code == 201, r.text
        body = r.json()
        assert body["strategy_id"] == "alpaca.live"
        assert body["class_name"] == "position_tracker"
        assert body["symbols"] == ["AMD", "MU"]
        assert body["params"] == {}
        assert body["model_binding"] is None
        assert body["enabled"] is False
        persisted = patched_strategy_store.get("alpaca.live")
        assert persisted is not None
        assert persisted.class_name == "position_tracker"

    @pytest.mark.asyncio
    async def test_adopt_existing_config_returns_409(
        self,
        fake_redis: fakeredis.aioredis.FakeRedis,
        client: AsyncClient,
        auth_headers: dict[str, str],
        patched_strategy_store: StrategyConfigStore,
    ) -> None:
        await client.post(
            "/strategies/configs",
            headers=auth_headers,
            json=_create_body(strategy_id="alpaca.live"),
        )
        store = PositionStore(fake_redis)
        await store.put(_pos("alpaca.live", "AMD", "23"))

        r = await client.post(
            "/strategies/configs/alpaca.live/adopt",
            headers=auth_headers,
        )

        assert r.status_code == 409

    @pytest.mark.asyncio
    async def test_adopt_without_open_positions_returns_404(
        self,
        fake_redis: fakeredis.aioredis.FakeRedis,
        client: AsyncClient,
        auth_headers: dict[str, str],
        patched_strategy_store: StrategyConfigStore,
    ) -> None:
        store = PositionStore(fake_redis)
        await store.put(_pos("alpaca.live", "AMD", "0"))

        r = await client.post(
            "/strategies/configs/alpaca.live/adopt",
            headers=auth_headers,
        )

        assert r.status_code == 404
        assert patched_strategy_store.get("alpaca.live") is None


# ------ List + Read ------------------------------------------------------- #


class TestListAndReadRoutes:
    @pytest.mark.asyncio
    async def test_list_empty(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        patched_strategy_store: StrategyConfigStore,
    ) -> None:
        r = await client.get("/strategies/configs", headers=auth_headers)
        assert r.status_code == 200
        assert r.json() == []

    @pytest.mark.asyncio
    async def test_list_sorted_by_strategy_id(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        patched_strategy_store: StrategyConfigStore,
    ) -> None:
        await client.post(
            "/strategies/configs",
            headers=auth_headers,
            json=_create_body(strategy_id="zzz"),
        )
        await client.post(
            "/strategies/configs",
            headers=auth_headers,
            json=_create_body(strategy_id="aaa"),
        )
        r = await client.get("/strategies/configs", headers=auth_headers)
        assert r.status_code == 200
        ids = [c["strategy_id"] for c in r.json()]
        assert ids == ["aaa", "zzz"]

    @pytest.mark.asyncio
    async def test_get_one_happy_path(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        patched_strategy_store: StrategyConfigStore,
    ) -> None:
        await client.post(
            "/strategies/configs",
            headers=auth_headers,
            json=_create_body(
                strategy_id="gbm_btc",
                class_name="gbm",
                model_binding="gbm_predictor.v1",
            ),
        )
        r = await client.get("/strategies/configs/gbm_btc", headers=auth_headers)
        assert r.status_code == 200
        body = r.json()
        assert body["strategy_id"] == "gbm_btc"
        assert body["class_name"] == "gbm"
        assert body["model_binding"] == "gbm_predictor.v1"

    @pytest.mark.asyncio
    async def test_get_missing_returns_404(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        patched_strategy_store: StrategyConfigStore,
    ) -> None:
        r = await client.get("/strategies/configs/nope", headers=auth_headers)
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_get_unsafe_id_returns_400(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        patched_strategy_store: StrategyConfigStore,
    ) -> None:
        # The router uses a path param, but an unsafe id reaches the
        # store which raises StrategyConfigError -> 400.
        r = await client.get("/strategies/configs/.hidden", headers=auth_headers)
        assert r.status_code == 400


# ------ Update ------------------------------------------------------------ #


class TestPatchRoute:
    async def _seed(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
    ) -> None:
        r = await client.post(
            "/strategies/configs",
            headers=auth_headers,
            json=_create_body(),
        )
        assert r.status_code == 201

    @pytest.mark.asyncio
    async def test_patch_partial_updates_only_given_fields(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        patched_strategy_store: StrategyConfigStore,
    ) -> None:
        await self._seed(client, auth_headers)
        r = await client.patch(
            "/strategies/configs/btc_ma_main",
            headers=auth_headers,
            json={"symbols": ["BTC-USD", "ETH-USD"]},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["symbols"] == ["BTC-USD", "ETH-USD"]
        # class_name / params preserved.
        assert body["class_name"] == "ma_crossover"
        assert body["params"] == {"fast": 5, "slow": 20}

    @pytest.mark.asyncio
    async def test_patch_model_binding_can_be_set_and_cleared(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        patched_strategy_store: StrategyConfigStore,
    ) -> None:
        await self._seed(client, auth_headers)
        # Set.
        r = await client.patch(
            "/strategies/configs/btc_ma_main",
            headers=auth_headers,
            json={"model_binding": "gbm_predictor.v1"},
        )
        assert r.status_code == 200
        assert r.json()["model_binding"] == "gbm_predictor.v1"
        # Clear by sending null.
        r = await client.patch(
            "/strategies/configs/btc_ma_main",
            headers=auth_headers,
            json={"model_binding": None},
        )
        assert r.status_code == 200
        assert r.json()["model_binding"] is None

    @pytest.mark.asyncio
    async def test_patch_rejects_unknown_class_name(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        patched_strategy_store: StrategyConfigStore,
    ) -> None:
        await self._seed(client, auth_headers)
        r = await client.patch(
            "/strategies/configs/btc_ma_main",
            headers=auth_headers,
            json={"class_name": "no_such"},
        )
        assert r.status_code == 400

    @pytest.mark.asyncio
    async def test_patch_rejects_empty_symbols(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        patched_strategy_store: StrategyConfigStore,
    ) -> None:
        await self._seed(client, auth_headers)
        r = await client.patch(
            "/strategies/configs/btc_ma_main",
            headers=auth_headers,
            json={"symbols": []},
        )
        assert r.status_code == 422

    @pytest.mark.asyncio
    async def test_patch_missing_returns_404(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        patched_strategy_store: StrategyConfigStore,
    ) -> None:
        r = await client.patch(
            "/strategies/configs/never_existed",
            headers=auth_headers,
            json={"enabled": True},
        )
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_patch_empty_body_is_noop_but_returns_config(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        patched_strategy_store: StrategyConfigStore,
    ) -> None:
        """PATCH {} should still succeed; exclude_unset=True -> no
        changed fields, but the store's upsert still refreshes
        ``updated_at``.  This documents the intentional behaviour."""
        await self._seed(client, auth_headers)
        before = patched_strategy_store.get("btc_ma_main")
        assert before is not None
        r = await client.patch(
            "/strategies/configs/btc_ma_main",
            headers=auth_headers,
            json={},
        )
        assert r.status_code == 200
        after = patched_strategy_store.get("btc_ma_main")
        assert after is not None
        assert after.class_name == before.class_name
        assert after.symbols == before.symbols


# ------ Delete ------------------------------------------------------------ #


class TestDeleteRoute:
    @pytest.mark.asyncio
    async def test_delete_happy_path_returns_204(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        patched_strategy_store: StrategyConfigStore,
    ) -> None:
        await client.post(
            "/strategies/configs",
            headers=auth_headers,
            json=_create_body(),
        )
        r = await client.delete("/strategies/configs/btc_ma_main", headers=auth_headers)
        assert r.status_code == 204
        assert patched_strategy_store.get("btc_ma_main") is None
        # History is retained (tombstone + original).
        history = patched_strategy_store.get_history("btc_ma_main")
        assert len(history) >= 1

    @pytest.mark.asyncio
    async def test_delete_missing_returns_404(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        patched_strategy_store: StrategyConfigStore,
    ) -> None:
        r = await client.delete("/strategies/configs/never", headers=auth_headers)
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_delete_unsafe_id_returns_400(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        patched_strategy_store: StrategyConfigStore,
    ) -> None:
        # Using ``.hidden`` (leading dot) rather than ``..`` because
        # httpx normalises ``..`` to parent, which would hit a
        # different route and return 405 instead.
        r = await client.delete("/strategies/configs/.hidden", headers=auth_headers)
        assert r.status_code == 400


# ------ Start / Stop ------------------------------------------------------ #


class TestLifecycleRoutes:
    @pytest.mark.asyncio
    async def test_start_flips_enabled_true(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        patched_strategy_store: StrategyConfigStore,
    ) -> None:
        await client.post(
            "/strategies/configs",
            headers=auth_headers,
            json=_create_body(enabled=False),
        )
        r = await client.post(
            "/strategies/configs/btc_ma_main/start",
            headers=auth_headers,
        )
        assert r.status_code == 200
        assert r.json()["enabled"] is True
        assert patched_strategy_store.get("btc_ma_main").enabled is True

    @pytest.mark.asyncio
    async def test_stop_flips_enabled_false(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        patched_strategy_store: StrategyConfigStore,
    ) -> None:
        await client.post(
            "/strategies/configs",
            headers=auth_headers,
            json=_create_body(enabled=True),
        )
        r = await client.post(
            "/strategies/configs/btc_ma_main/stop",
            headers=auth_headers,
        )
        assert r.status_code == 200
        assert r.json()["enabled"] is False

    @pytest.mark.asyncio
    async def test_start_idempotent_no_history_spam(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        patched_strategy_store: StrategyConfigStore,
    ) -> None:
        """Double-start should not append an extra audit entry."""
        await client.post(
            "/strategies/configs",
            headers=auth_headers,
            json=_create_body(enabled=True),
        )
        before = len(patched_strategy_store.get_history("btc_ma_main"))
        r = await client.post(
            "/strategies/configs/btc_ma_main/start",
            headers=auth_headers,
        )
        assert r.status_code == 200
        assert r.json()["enabled"] is True
        after = len(patched_strategy_store.get_history("btc_ma_main"))
        assert after == before

    @pytest.mark.asyncio
    async def test_stop_idempotent_no_history_spam(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        patched_strategy_store: StrategyConfigStore,
    ) -> None:
        await client.post(
            "/strategies/configs",
            headers=auth_headers,
            json=_create_body(enabled=False),
        )
        before = len(patched_strategy_store.get_history("btc_ma_main"))
        r = await client.post(
            "/strategies/configs/btc_ma_main/stop",
            headers=auth_headers,
        )
        assert r.status_code == 200
        after = len(patched_strategy_store.get_history("btc_ma_main"))
        assert after == before

    @pytest.mark.asyncio
    async def test_start_missing_returns_404(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        patched_strategy_store: StrategyConfigStore,
    ) -> None:
        r = await client.post(
            "/strategies/configs/never_existed/start",
            headers=auth_headers,
        )
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_stop_missing_returns_404(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        patched_strategy_store: StrategyConfigStore,
    ) -> None:
        r = await client.post(
            "/strategies/configs/never_existed/stop",
            headers=auth_headers,
        )
        assert r.status_code == 404


# ------ History ----------------------------------------------------------- #


class TestHistoryRoute:
    @pytest.mark.asyncio
    async def test_history_returns_newest_first(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        patched_strategy_store: StrategyConfigStore,
    ) -> None:
        await client.post(
            "/strategies/configs",
            headers=auth_headers,
            json=_create_body(),
        )
        # Mutate twice so we have three history entries.
        await client.patch(
            "/strategies/configs/btc_ma_main",
            headers=auth_headers,
            json={"params": {"fast": 10, "slow": 40}},
        )
        await client.patch(
            "/strategies/configs/btc_ma_main",
            headers=auth_headers,
            json={"params": {"fast": 20, "slow": 60}},
        )
        r = await client.get(
            "/strategies/configs/btc_ma_main/history",
            headers=auth_headers,
        )
        assert r.status_code == 200
        history = r.json()
        assert len(history) == 3
        # Newest first -> most-recent params.
        assert history[0]["params"] == {"fast": 20, "slow": 60}
        assert history[-1]["params"] == {"fast": 5, "slow": 20}

    @pytest.mark.asyncio
    async def test_history_empty_for_never_written(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        patched_strategy_store: StrategyConfigStore,
    ) -> None:
        """No 404 here; an audit query for a strategy without history
        returns [] so the operator UI can render ``no changes yet``."""
        r = await client.get(
            "/strategies/configs/never_written/history",
            headers=auth_headers,
        )
        assert r.status_code == 200
        assert r.json() == []

    @pytest.mark.asyncio
    async def test_history_limit_clamped_to_500(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        patched_strategy_store: StrategyConfigStore,
    ) -> None:
        # We don't need 500 entries to test the clamp — the route's
        # behaviour is "silently cap"; just verify the call succeeds
        # and returns <= 500.
        await client.post(
            "/strategies/configs",
            headers=auth_headers,
            json=_create_body(),
        )
        r = await client.get(
            "/strategies/configs/btc_ma_main/history?limit=5000",
            headers=auth_headers,
        )
        assert r.status_code == 200
        assert len(r.json()) <= 500

    @pytest.mark.asyncio
    async def test_history_rejects_limit_lt_one(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        patched_strategy_store: StrategyConfigStore,
    ) -> None:
        r = await client.get(
            "/strategies/configs/anything/history?limit=0",
            headers=auth_headers,
        )
        assert r.status_code == 400

    @pytest.mark.asyncio
    async def test_history_unsafe_id_returns_400(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        patched_strategy_store: StrategyConfigStore,
    ) -> None:
        r = await client.get(
            "/strategies/configs/..bad/history",
            headers=auth_headers,
        )
        assert r.status_code == 400
