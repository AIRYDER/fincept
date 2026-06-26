"""Tests for the /backtest endpoints.

Covers:
  * /backtest/strategies returns the registered strategy list
  * /backtest/run on a tiny in-memory parquet produces a report
  * /backtest/runs lists persisted manifests newest-first
  * /backtest/runs/{id} returns the matching report
  * /backtest/runs/{id} 404 on unknown id
  * Bad inputs (missing parquet, unknown strategy, bad venue) -> 400
  * Approved-roots gate: approved path runs; unapproved / traversal -> 422

A fresh ``reports/backtests`` root is created in tmp_path and patched
into both the route + the runner so tests don't pollute the workspace.
"""

from __future__ import annotations

import pathlib

import polars as pl
import pytest
from httpx import AsyncClient


def _write_synth_parquet(path: pathlib.Path, n_bars: int = 60) -> None:
    """Write a tiny BTC-USD bars parquet just big enough for the engine."""
    base_ts = 1_700_000_000_000_000_000  # arbitrary nanosecond epoch
    rows = []
    for i in range(n_bars):
        # Walk close around 50,000 with a small linear trend so MA
        # crossover actually triggers at least once.
        close = 50_000.0 + i * 5.0
        rows.append(
            {
                "symbol": "BTC-USD",
                "ts_event": base_ts + i * 60_000_000_000,
                "open": close - 1.0,
                "high": close + 5.0,
                "low": close - 5.0,
                "close": close,
                "volume": 100.0,
            }
        )
    pl.DataFrame(rows).write_parquet(path)


@pytest.fixture(autouse=True)
def _patch_reports_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> pathlib.Path:
    """Redirect the runner + route at a tmp_path reports root."""
    root = tmp_path / "backtest_reports"
    root.mkdir()
    monkeypatch.setattr("backtester.runner.REPORTS_ROOT", root)
    monkeypatch.setattr("api.routes.backtest.REPORTS_ROOT", root)
    return root


@pytest.fixture
def synth_parquet(tmp_path: pathlib.Path) -> pathlib.Path:
    path = tmp_path / "synth.parquet"
    _write_synth_parquet(path)
    return path


@pytest.fixture
def approved_data_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> pathlib.Path:
    """Admit ``tmp_path`` as an approved data root for the duration of the test.

    The production default approved roots (``data``, ``models``) are
    relative to the repo working directory, which is not where pytest's
    ``tmp_path`` lives.  Rather than write a real parquet under
    ``<repo>/data/captures`` (which would pollute the workspace and race
    with other tests), we point ``FINCEPT_APPROVED_DATA_ROOTS`` at the
    per-test tmp_path so the gate admits the synth parquet while still
    rejecting absolute paths outside tmp_path (e.g. ``/etc/passwd``).
    """
    monkeypatch.setenv("FINCEPT_APPROVED_DATA_ROOTS", str(tmp_path))
    return tmp_path


# --------------------------------------------------------------------------- #
# /backtest/strategies                                                        #
# --------------------------------------------------------------------------- #


class TestStrategiesEndpoint:
    @pytest.mark.asyncio
    async def test_requires_auth(self, client: AsyncClient) -> None:
        response = await client.get("/backtest/strategies")
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_returns_known_strategies(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        response = await client.get("/backtest/strategies", headers=auth_headers)
        assert response.status_code == 200
        body = response.json()
        keys = {row["key"] for row in body["strategies"]}
        assert "buy_and_hold" in keys
        assert "ma_crossover" in keys
        # Each row carries enough info for the dashboard to render.
        for row in body["strategies"]:
            assert "key" in row
            assert "class_name" in row
            assert "strategy_id" in row
            assert "description" in row


# --------------------------------------------------------------------------- #
# POST /backtest/run                                                          #
# --------------------------------------------------------------------------- #


class TestRunEndpoint:
    @pytest.mark.asyncio
    async def test_run_buy_and_hold(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        synth_parquet: pathlib.Path,
        approved_data_root: pathlib.Path,
    ) -> None:
        body = {
            "bars_path": str(synth_parquet),
            "strategy": "buy_and_hold",
            "strategy_params": {"per_symbol_notional": 5000},
            "starting_cash": 50_000,
            "freq": "1m",
        }
        response = await client.post("/backtest/run", json=body, headers=auth_headers)
        assert response.status_code == 200, response.text
        payload = response.json()
        assert "run_id" in payload
        assert payload["report"]["n_bars"] > 0
        assert payload["report"]["n_fills"] >= 1  # buy_and_hold opens once
        assert payload["report"]["starting_cash"] == 50_000
        assert payload["manifest"]["strategy_name"] == "buy_and_hold"
        assert payload["manifest"]["status"] == "complete"

    @pytest.mark.asyncio
    async def test_run_unknown_strategy_returns_400(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        synth_parquet: pathlib.Path,
        approved_data_root: pathlib.Path,
    ) -> None:
        body = {
            "bars_path": str(synth_parquet),
            "strategy": "definitely_not_real",
        }
        response = await client.post("/backtest/run", json=body, headers=auth_headers)
        assert response.status_code == 400
        assert "unknown strategy" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_run_missing_parquet_returns_400(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        tmp_path: pathlib.Path,
        approved_data_root: pathlib.Path,
    ) -> None:
        body = {
            "bars_path": str(tmp_path / "nope.parquet"),
            "strategy": "buy_and_hold",
        }
        response = await client.post("/backtest/run", json=body, headers=auth_headers)
        assert response.status_code == 400
        assert "does not exist" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_run_bad_venue_returns_400(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        synth_parquet: pathlib.Path,
        approved_data_root: pathlib.Path,
    ) -> None:
        body = {
            "bars_path": str(synth_parquet),
            "strategy": "buy_and_hold",
            "venue": "definitely_not_a_venue",
        }
        response = await client.post("/backtest/run", json=body, headers=auth_headers)
        assert response.status_code == 400


# --------------------------------------------------------------------------- #
# GET /backtest/runs + /runs/{id}                                             #
# --------------------------------------------------------------------------- #


class TestRunsListAndDetail:
    @pytest.mark.asyncio
    async def test_runs_empty_when_no_runs(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        response = await client.get("/backtest/runs", headers=auth_headers)
        assert response.status_code == 200
        body = response.json()
        assert body["runs"] == []
        assert body["summary"]["count"] == 0

    @pytest.mark.asyncio
    async def test_runs_lists_after_run(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        synth_parquet: pathlib.Path,
        approved_data_root: pathlib.Path,
    ) -> None:
        body = {
            "bars_path": str(synth_parquet),
            "strategy": "buy_and_hold",
        }
        run_resp = await client.post("/backtest/run", json=body, headers=auth_headers)
        assert run_resp.status_code == 200
        run_id = run_resp.json()["run_id"]

        list_resp = await client.get("/backtest/runs", headers=auth_headers)
        assert list_resp.status_code == 200
        runs = list_resp.json()["runs"]
        assert len(runs) == 1
        assert runs[0]["run_id"] == run_id

        detail_resp = await client.get(f"/backtest/runs/{run_id}", headers=auth_headers)
        assert detail_resp.status_code == 200
        detail = detail_resp.json()
        assert detail["run_id"] == run_id
        assert detail["report"]["n_bars"] > 0
        assert detail["manifest"]["run_id"] == run_id

    @pytest.mark.asyncio
    async def test_run_detail_404_on_unknown(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        response = await client.get(
            "/backtest/runs/does-not-exist", headers=auth_headers
        )
        assert response.status_code == 404


# --------------------------------------------------------------------------- #
# Approved-roots gate (todo 7)                                                #
# --------------------------------------------------------------------------- #


class TestApprovedRootsGate:
    """The approved-roots gate is layered on top of the existing checks.

    Happy path: a parquet inside an approved root runs as before.
    Failure paths:
      * absolute path outside every approved root -> 422
        ``{"detail": ..., "code": "approved_roots_violation"}``
      * traversal (``..``) anywhere in the candidate -> 422
    The existing 400 "does not exist" / "unknown strategy" checks still
    fire for paths that pass the gate but miss on disk, proving the new
    check is layered on top rather than replacing the old ones.
    """

    @pytest.mark.asyncio
    async def test_approved_path_runs_as_before(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        synth_parquet: pathlib.Path,
        approved_data_root: pathlib.Path,
    ) -> None:
        """A parquet inside an approved root produces a normal 200 report."""
        body = {
            "bars_path": str(synth_parquet),
            "strategy": "buy_and_hold",
        }
        response = await client.post("/backtest/run", json=body, headers=auth_headers)
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["report"]["n_bars"] > 0
        assert payload["manifest"]["strategy_name"] == "buy_and_hold"

    @pytest.mark.asyncio
    async def test_absolute_path_outside_roots_returns_422(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        approved_data_root: pathlib.Path,
    ) -> None:
        """``/etc/passwd`` is outside every approved root -> 422."""
        body = {
            "bars_path": "/etc/passwd",
            "strategy": "buy_and_hold",
        }
        response = await client.post("/backtest/run", json=body, headers=auth_headers)
        assert response.status_code == 422, response.text
        payload = response.json()
        assert payload["code"] == "approved_roots_violation"
        assert "detail" in payload
        # The approved-roots list is never echoed in the message.
        assert "approved_roots_violation" not in payload["detail"]
        # The finer reason lives in the response header for operators.
        assert response.headers.get("X-Approved-Roots-Code") == "outside_root"

    @pytest.mark.asyncio
    async def test_traversal_path_returns_422(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        approved_data_root: pathlib.Path,
    ) -> None:
        """``../etc/passwd`` contains a ``..`` component -> 422."""
        body = {
            "bars_path": "../etc/passwd",
            "strategy": "buy_and_hold",
        }
        response = await client.post("/backtest/run", json=body, headers=auth_headers)
        assert response.status_code == 422, response.text
        payload = response.json()
        assert payload["code"] == "approved_roots_violation"
        assert response.headers.get("X-Approved-Roots-Code") == "traversal"

    @pytest.mark.asyncio
    async def test_gate_layers_on_top_of_existence_check(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        approved_data_root: pathlib.Path,
    ) -> None:
        """A path inside the approved root but missing on disk -> 400 (not 422).

        Proves the new gate does not replace the existing
        ``bars_path does not exist`` check; it runs before it and only
        short-circuits on an approved-roots violation.
        """
        body = {
            "bars_path": str(approved_data_root / "nope.parquet"),
            "strategy": "buy_and_hold",
        }
        response = await client.post("/backtest/run", json=body, headers=auth_headers)
        assert response.status_code == 400
        assert "does not exist" in response.json()["detail"]
