"""
Tests for the symbol-search typeahead.

Two layers:

  * Pure scoring (``search_symbols``) — no FastAPI, no DB.  Most of
    the matching algorithm's invariants are pinned down here.
  * The integrated route (``GET /data/symbols/search``) — confirms
    auth + parameter validation + JSON serialisation.
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient

from api.symbol_search import (
    WELL_KNOWN,
    SymbolMatch,
    _levenshtein_le1,
    search_symbols,
)

# --------------------------------------------------------------------------- #
# Pure helpers                                                                #
# --------------------------------------------------------------------------- #


def test_levenshtein_handles_identical() -> None:
    assert _levenshtein_le1("aapl", "aapl")


@pytest.mark.parametrize(
    "a,b",
    [
        ("aapl", "aaple"),  # insertion
        ("nvda", "nvd"),  # deletion
        ("nvda", "nvds"),  # substitution
        ("", "a"),  # empty + 1
    ],
)
def test_levenshtein_one_edit(a: str, b: str) -> None:
    assert _levenshtein_le1(a, b)


@pytest.mark.parametrize(
    "a,b",
    [
        ("aapl", "msft"),  # totally different
        ("nvda", "nvdaxy"),  # 2 insertions
        ("abc", "xyz"),  # 3 substitutions
    ],
)
def test_levenshtein_rejects_two_or_more_edits(a: str, b: str) -> None:
    assert not _levenshtein_le1(a.lower(), b.lower())


# --------------------------------------------------------------------------- #
# search_symbols                                                              #
# --------------------------------------------------------------------------- #


def test_search_empty_query_returns_empty_list() -> None:
    out = search_symbols("", universe_rows=[])
    assert out == []


def test_search_finds_well_known_ticker_by_lowercase_query() -> None:
    """Operator types ``nvda`` -> NVDA appears at the top."""
    out = search_symbols("nvda", universe_rows=[])
    assert len(out) >= 1
    assert out[0].symbol == "NVDA"
    assert out[0].source == "well_known"
    assert out[0].score >= 1000  # exact symbol match


def test_search_prefix_beats_substring() -> None:
    """``aap`` -> AAPL ranks above any later-substring match."""
    out = search_symbols("aap", universe_rows=[])
    assert out[0].symbol == "AAPL"


def test_search_returns_results_sorted_by_score_desc() -> None:
    """Scores are non-increasing across the result list."""
    out = search_symbols("a", universe_rows=[], limit=20)
    scores = [m.score for m in out]
    assert scores == sorted(scores, reverse=True)


def test_search_universe_overrides_well_known() -> None:
    """A universe row with the same symbol takes precedence."""
    universe = [
        {
            "symbol": "NVDA",
            "name": "NVIDIA Corp (custom name)",
            "asset_class": "equity",
        }
    ]
    out = search_symbols("nvda", universe_rows=universe)
    assert out[0].symbol == "NVDA"
    assert out[0].source == "universe"
    assert "custom name" in out[0].name


def test_search_finds_company_name_substring() -> None:
    """Typing the company name (``apple``) finds AAPL."""
    out = search_symbols("apple", universe_rows=[])
    symbols = [m.symbol for m in out]
    assert "AAPL" in symbols


def test_search_typo_within_one_edit_still_matches() -> None:
    """``aaple`` (typo) still finds AAPL via the 1-edit tier."""
    out = search_symbols("aaple", universe_rows=[])
    symbols = [m.symbol for m in out]
    assert "AAPL" in symbols


def test_search_respects_limit() -> None:
    """The ``limit`` parameter caps the result count."""
    out = search_symbols("a", universe_rows=[], limit=3)
    assert len(out) == 3


def test_search_short_symbol_breaks_ties_first() -> None:
    """For tied scores, a shorter symbol comes first.

    Both NVDA and an extra "NVDAXY" candidate prefix-match "nvda",
    score=500 + length-bonus.  NVDA (4 chars) should win over NVDAXY
    (6 chars) on the length tiebreak.
    """
    extras = [{"symbol": "NVDAXY", "name": "Fake Co.", "asset_class": "equity"}]
    out = search_symbols("nvda", universe_rows=[], extras=extras)
    assert out[0].symbol == "NVDA"


def test_search_handles_crypto_pair() -> None:
    """Typing ``btc`` resolves to BTC-USD."""
    out = search_symbols("btc", universe_rows=[])
    symbols = [m.symbol for m in out]
    assert "BTC-USD" in symbols


def test_search_match_dataclass_fields() -> None:
    """Every result is a fully-populated SymbolMatch."""
    out = search_symbols("nvda", universe_rows=[])
    for match in out:
        assert isinstance(match, SymbolMatch)
        assert match.symbol
        assert match.name
        assert match.asset_class
        assert match.score > 0
        assert match.source in {"universe", "well_known"}


def test_well_known_list_is_non_empty_and_unique() -> None:
    """Sanity check on the curated list (catches editor-merge regressions)."""
    syms = [row["symbol"] for row in WELL_KNOWN]
    assert len(syms) > 50
    assert len(syms) == len(set(syms)), "WELL_KNOWN has duplicates"


# --------------------------------------------------------------------------- #
# Integrated /data/symbols/search                                             #
# --------------------------------------------------------------------------- #


async def test_symbol_search_requires_auth(client: AsyncClient) -> None:
    response = await client.get("/data/symbols/search?q=nvda")
    assert response.status_code == 401


async def test_symbol_search_returns_well_known_on_empty_universe(
    client: AsyncClient,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty universe -> well-known list still surfaces NVDA for the query."""

    async def empty_universe(
        *, asset_class: str | None = None, active_only: bool = True
    ) -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr("api.routes.data.read_universe", empty_universe)

    response = await client.get(
        "/data/symbols/search", headers=auth_headers, params={"q": "nvda"}
    )
    assert response.status_code == 200
    body = response.json()
    assert isinstance(body, list)
    assert body[0]["symbol"] == "NVDA"
    assert body[0]["source"] == "well_known"
    assert body[0]["score"] >= 1000


async def test_symbol_search_includes_universe_overrides(
    client: AsyncClient,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A universe symbol absent from WELL_KNOWN should still be findable."""

    async def universe_with_custom(
        *, asset_class: str | None = None, active_only: bool = True
    ) -> list[dict[str, Any]]:
        return [
            {
                "symbol": "FOO_CUSTOM",
                "name": "Foo Custom Holdings",
                "asset_class": "equity",
                "active": True,
            }
        ]

    monkeypatch.setattr("api.routes.data.read_universe", universe_with_custom)

    response = await client.get(
        "/data/symbols/search",
        headers=auth_headers,
        params={"q": "foo"},
    )
    body = response.json()
    symbols = [r["symbol"] for r in body]
    assert "FOO_CUSTOM" in symbols
    custom = next(r for r in body if r["symbol"] == "FOO_CUSTOM")
    assert custom["source"] == "universe"


async def test_symbol_search_respects_limit_param(
    client: AsyncClient,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def empty_universe(
        *, asset_class: str | None = None, active_only: bool = True
    ) -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr("api.routes.data.read_universe", empty_universe)

    response = await client.get(
        "/data/symbols/search",
        headers=auth_headers,
        params={"q": "a", "limit": 3},
    )
    assert response.status_code == 200
    body = response.json()
    assert len(body) <= 3


async def test_symbol_search_rejects_empty_query(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    response = await client.get(
        "/data/symbols/search", headers=auth_headers, params={"q": ""}
    )
    # FastAPI/Pydantic returns 422 for min_length violations.
    assert response.status_code == 422


async def test_symbol_search_rejects_too_long_query(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    response = await client.get(
        "/data/symbols/search",
        headers=auth_headers,
        params={"q": "a" * 25},
    )
    assert response.status_code == 422
