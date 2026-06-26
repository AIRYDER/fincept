"""
Tests for ``fincept_core.strategy_config``.

The store mirrors ``api.promotions``: filesystem-backed atomic writes
plus an append-only history.  These tests cover validation, the
round-trip property, history ordering, the idempotent toggle, and
the tolerance behaviours that keep operator-facing tooling alive
when an on-disk file is corrupt or hand-edited.
"""

from __future__ import annotations

import dataclasses
import json
import pathlib
import time

import pytest

from fincept_core.strategy_config import (
    StrategyConfig,
    StrategyConfigError,
    StrategyConfigStore,
    get_strategy_config_store,
    reset_strategy_config_store,
)

# --------------------------------------------------------------------------- #
# Fixtures                                                                   #
# --------------------------------------------------------------------------- #


@pytest.fixture
def store(tmp_path: pathlib.Path) -> StrategyConfigStore:
    """Hermetic store rooted under ``tmp_path``."""
    return StrategyConfigStore(configs_dir=tmp_path / "strategies")


def _make_config(
    strategy_id: str = "btc_ma_main",
    *,
    class_name: str = "ma_crossover",
    symbols: list[str] | None = None,
    params: dict[str, object] | None = None,
    model_binding: str | None = None,
    enabled: bool = False,
    created_at: float = 0.0,
    updated_at: float = 0.0,
) -> StrategyConfig:
    """Helper that returns a fresh StrategyConfig with sensible defaults."""
    return StrategyConfig(
        strategy_id=strategy_id,
        class_name=class_name,
        symbols=symbols or ["BTC-USD"],
        params=params or {"fast": 5, "slow": 20},
        model_binding=model_binding,
        enabled=enabled,
        created_at=created_at,
        updated_at=updated_at,
    )


# --------------------------------------------------------------------------- #
# StrategyConfig dataclass                                                    #
# --------------------------------------------------------------------------- #


class TestStrategyConfig:
    def test_round_trip_dict(self) -> None:
        cfg = _make_config(
            symbols=["BTC-USD", "ETH-USD"],
            params={"fast": 3, "slow": 10},
            model_binding="gbm_predictor.v1",
            enabled=True,
            created_at=1.0,
            updated_at=2.0,
        )
        again = StrategyConfig.from_dict(cfg.to_dict())
        assert again == cfg

    def test_to_dict_returns_copies_not_views(self) -> None:
        # Mutating the returned dict's nested containers must not
        # change the frozen record.  A naive ``dataclasses.asdict``
        # would expose live references; our explicit to_dict copies.
        cfg = _make_config(symbols=["BTC-USD"], params={"fast": 5})
        snap = cfg.to_dict()
        snap["symbols"].append("ETH-USD")
        snap["params"]["fast"] = 99
        assert cfg.symbols == ["BTC-USD"]
        assert cfg.params == {"fast": 5}

    def test_from_dict_tolerates_missing_optional_keys(self) -> None:
        # Older on-disk versions may pre-date model_binding (Phase F).
        # The reader must default sensibly rather than KeyError.
        partial = {
            "strategy_id": "x",
            "class_name": "buy_and_hold",
        }
        cfg = StrategyConfig.from_dict(partial)
        assert cfg.symbols == []
        assert cfg.params == {}
        assert cfg.model_binding is None
        assert cfg.enabled is False
        assert cfg.created_at == 0.0
        assert cfg.updated_at == 0.0

    def test_from_dict_requires_id_and_class(self) -> None:
        with pytest.raises(KeyError):
            StrategyConfig.from_dict({"class_name": "x"})
        with pytest.raises(KeyError):
            StrategyConfig.from_dict({"strategy_id": "x"})

    def test_from_dict_treats_empty_string_binding_as_none(self) -> None:
        # The dashboard's "unbind" form may submit "" rather than
        # null; the reader normalises both to None.
        cfg = StrategyConfig.from_dict(
            {
                "strategy_id": "x",
                "class_name": "buy_and_hold",
                "model_binding": "",
            }
        )
        assert cfg.model_binding is None


# --------------------------------------------------------------------------- #
# Validation                                                                 #
# --------------------------------------------------------------------------- #


class TestValidation:
    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "btc/main",
            "btc\\main",
            "..",
            ".hidden",
            "btc:main",
            "btc*",
            'btc"main',
            "btc<main",
            "btc>main",
            "btc|main",
            "btc?main",
        ],
    )
    def test_get_rejects_unsafe_id(self, store: StrategyConfigStore, bad: str) -> None:
        with pytest.raises(StrategyConfigError):
            store.get(bad)

    @pytest.mark.parametrize(
        "bad",
        ["", "../escape", ".", ".."],
    )
    def test_upsert_rejects_unsafe_id(self, store: StrategyConfigStore, bad: str) -> None:
        cfg = _make_config(strategy_id=bad)
        with pytest.raises(StrategyConfigError):
            store.upsert(cfg)

    def test_set_enabled_rejects_unsafe_id(self, store: StrategyConfigStore) -> None:
        with pytest.raises(StrategyConfigError):
            store.set_enabled("../escape", enabled=True)

    def test_delete_rejects_unsafe_id(self, store: StrategyConfigStore) -> None:
        with pytest.raises(StrategyConfigError):
            store.delete("../escape")


# --------------------------------------------------------------------------- #
# Round-trip + listing                                                       #
# --------------------------------------------------------------------------- #


class TestUpsertAndGet:
    def test_get_missing_returns_none(self, store: StrategyConfigStore) -> None:
        assert store.get("nope") is None

    def test_upsert_then_get_round_trip(self, store: StrategyConfigStore) -> None:
        cfg = _make_config()
        sealed = store.upsert(cfg)
        # Returned record has timestamps populated.
        assert sealed.created_at > 0
        assert sealed.updated_at > 0
        assert sealed.created_at <= sealed.updated_at
        # On-disk read returns the same sealed record.
        again = store.get(cfg.strategy_id)
        assert again == sealed

    def test_upsert_preserves_created_at_on_update(self, store: StrategyConfigStore) -> None:
        first = store.upsert(_make_config())
        time.sleep(0.01)
        # Caller-provided created_at on the second upsert is ignored;
        # the existing one is preserved so the audit shows when the
        # strategy was originally created.
        updated = store.upsert(
            _make_config(
                params={"fast": 7, "slow": 25},
                created_at=999.0,
            )
        )
        assert updated.created_at == first.created_at
        assert updated.updated_at > first.updated_at

    def test_upsert_overwrites_caller_updated_at(self, store: StrategyConfigStore) -> None:
        # Even an explicit caller value loses to wall-clock now so the
        # host can rely on monotonic ordering for change detection.
        sealed = store.upsert(_make_config(updated_at=time.time() - 10_000))
        # ``updated_at`` is "now" at write time, not the input value.
        assert sealed.updated_at > time.time() - 5

    def test_list_all_empty_dir(self, store: StrategyConfigStore) -> None:
        assert store.list_all() == []

    def test_list_all_returns_sorted_by_id(self, store: StrategyConfigStore) -> None:
        store.upsert(_make_config(strategy_id="c_strategy"))
        store.upsert(_make_config(strategy_id="a_strategy"))
        store.upsert(_make_config(strategy_id="b_strategy"))
        ids = [c.strategy_id for c in store.list_all()]
        assert ids == ["a_strategy", "b_strategy", "c_strategy"]

    def test_list_all_skips_history_files(
        self, store: StrategyConfigStore, tmp_path: pathlib.Path
    ) -> None:
        # An upsert produces both a *.json and a *.history.jsonl.
        # list_all must enumerate only the *.json (current state).
        store.upsert(_make_config(strategy_id="alpha"))
        store.upsert(_make_config(strategy_id="alpha", enabled=True))
        listed = store.list_all()
        assert len(listed) == 1
        assert listed[0].strategy_id == "alpha"

    def test_list_all_skips_unsafe_named_files(self, store: StrategyConfigStore) -> None:
        # If someone drops a file named ".hidden.json" in the configs
        # dir it must be ignored (its stem starts with '.', which our
        # validator rejects).
        store.upsert(_make_config(strategy_id="visible"))
        store.configs_dir.mkdir(parents=True, exist_ok=True)
        (store.configs_dir / ".hidden.json").write_text("{}")
        listed = store.list_all()
        assert [c.strategy_id for c in listed] == ["visible"]


# --------------------------------------------------------------------------- #
# History                                                                     #
# --------------------------------------------------------------------------- #


class TestHistory:
    def test_get_history_empty(self, store: StrategyConfigStore) -> None:
        assert store.get_history("never") == []

    def test_history_records_each_upsert(self, store: StrategyConfigStore) -> None:
        first = store.upsert(_make_config())
        time.sleep(0.005)
        second = store.upsert(_make_config(enabled=True))
        history = store.get_history("btc_ma_main")
        # Newest first.
        assert len(history) == 2
        assert history[0].enabled is True
        assert history[1].enabled is False
        # ``updated_at`` is monotonic.
        assert history[0].updated_at > history[1].updated_at
        # ``created_at`` matches the first record on both lines.
        assert history[0].created_at == first.created_at
        assert history[1].created_at == first.created_at
        # The second is what's currently on disk.
        on_disk = store.get("btc_ma_main")
        assert on_disk == second

    def test_history_limit_truncates(self, store: StrategyConfigStore) -> None:
        for i in range(5):
            store.upsert(_make_config(params={"fast": i, "slow": 30}))
        history = store.get_history("btc_ma_main", limit=2)
        assert len(history) == 2
        # Newest first: params['fast'] == 4 most recent.
        assert history[0].params["fast"] == 4
        assert history[1].params["fast"] == 3

    def test_history_rejects_zero_limit(self, store: StrategyConfigStore) -> None:
        with pytest.raises(StrategyConfigError, match="limit"):
            store.get_history("anything", limit=0)

    def test_history_skips_malformed_lines(self, store: StrategyConfigStore) -> None:
        store.upsert(_make_config())
        # Hand-corrupt a line into the middle of the history file to
        # simulate a crash mid-write or a hand edit.
        path = store._history_path("btc_ma_main")
        with path.open("a", encoding="utf-8") as f:
            f.write("not-json\n")
            f.write('{"strategy_id": "btc_ma_main"}\n')  # missing class_name
        store.upsert(_make_config(enabled=True))
        history = store.get_history("btc_ma_main")
        # Two valid records survive; the corrupt lines are skipped.
        assert len(history) == 2
        assert all(c.strategy_id == "btc_ma_main" for c in history)


# --------------------------------------------------------------------------- #
# set_enabled                                                                 #
# --------------------------------------------------------------------------- #


class TestSetEnabled:
    def test_set_enabled_missing_raises(self, store: StrategyConfigStore) -> None:
        with pytest.raises(StrategyConfigError, match="not found"):
            store.set_enabled("nope", enabled=True)

    def test_set_enabled_flips_flag(self, store: StrategyConfigStore) -> None:
        store.upsert(_make_config(enabled=False))
        toggled = store.set_enabled("btc_ma_main", enabled=True)
        assert toggled.enabled is True
        assert store.get("btc_ma_main") == toggled  # type: ignore[arg-type]

    def test_set_enabled_idempotent(self, store: StrategyConfigStore) -> None:
        # Setting to the current value is a no-op: same record back,
        # no new history line, no rewritten timestamp.
        original = store.upsert(_make_config(enabled=True))
        history_before = len(store.get_history("btc_ma_main"))
        again = store.set_enabled("btc_ma_main", enabled=True)
        history_after = len(store.get_history("btc_ma_main"))
        assert again == original
        assert history_after == history_before


# --------------------------------------------------------------------------- #
# Delete                                                                      #
# --------------------------------------------------------------------------- #


class TestDelete:
    def test_delete_missing_returns_false(self, store: StrategyConfigStore) -> None:
        assert store.delete("nope") is False

    def test_delete_removes_file_and_returns_true(self, store: StrategyConfigStore) -> None:
        store.upsert(_make_config())
        assert store.delete("btc_ma_main") is True
        assert store.get("btc_ma_main") is None

    def test_delete_appends_tombstone_to_history(self, store: StrategyConfigStore) -> None:
        store.upsert(_make_config(enabled=True))
        store.delete("btc_ma_main")
        history = store.get_history("btc_ma_main")
        # Newest entry is the tombstone.
        assert history[0].class_name == "(deleted)"
        assert history[0].enabled is False
        # The original record is still in history (entry 1).
        assert history[1].class_name == "ma_crossover"

    def test_delete_then_recreate(self, store: StrategyConfigStore) -> None:
        first = store.upsert(_make_config())
        store.delete("btc_ma_main")
        time.sleep(0.005)
        # A new upsert with the same id must NOT inherit the old
        # created_at -- the tombstone broke continuity, so this is
        # logically a fresh record.
        second = store.upsert(_make_config())
        assert second.created_at > first.created_at


# --------------------------------------------------------------------------- #
# Atomic write + corruption tolerance                                         #
# --------------------------------------------------------------------------- #


class TestAtomicWrite:
    def test_atomic_write_leaves_no_tmp_file(self, store: StrategyConfigStore) -> None:
        store.upsert(_make_config())
        # The rename completes fully; no .tmp left around.
        leftover = list(store.configs_dir.glob("*.tmp"))
        assert leftover == []

    def test_get_tolerates_malformed_json(self, store: StrategyConfigStore) -> None:
        # An operator hand-editing the file may leave it invalid;
        # the dashboard should see "no config" rather than a 500.
        store.upsert(_make_config())
        path = store._config_path("btc_ma_main")
        path.write_text("{this is not json")
        assert store.get("btc_ma_main") is None

    def test_get_tolerates_missing_required_key(self, store: StrategyConfigStore) -> None:
        store.upsert(_make_config())
        path = store._config_path("btc_ma_main")
        path.write_text(json.dumps({"strategy_id": "btc_ma_main"}))
        # Required ``class_name`` missing -> treated as malformed.
        assert store.get("btc_ma_main") is None


# --------------------------------------------------------------------------- #
# Module singleton                                                            #
# --------------------------------------------------------------------------- #


class TestSingleton:
    def test_get_returns_lazy_singleton(self, tmp_path: pathlib.Path) -> None:
        # Reset to a hermetic dir, then verify the public accessor
        # returns the same store on repeat calls.
        reset_strategy_config_store(configs_dir=tmp_path / "strategies")
        s1 = get_strategy_config_store()
        s2 = get_strategy_config_store()
        assert s1 is s2

    def test_reset_swaps_underlying_dir(self, tmp_path: pathlib.Path) -> None:
        a = tmp_path / "a"
        b = tmp_path / "b"
        s1 = reset_strategy_config_store(configs_dir=a)
        s1.upsert(_make_config(strategy_id="alpha"))
        s2 = reset_strategy_config_store(configs_dir=b)
        # New store with new dir sees nothing.
        assert s2.list_all() == []
        # And the singleton accessor returns the new one.
        assert get_strategy_config_store() is s2


# --------------------------------------------------------------------------- #
# Equality + dataclasses.replace ergonomics                                   #
# --------------------------------------------------------------------------- #


def test_dataclass_replace_works_for_partial_updates() -> None:
    cfg = _make_config(enabled=False)
    flipped = dataclasses.replace(cfg, enabled=True)
    assert flipped.enabled is True
    assert flipped.strategy_id == cfg.strategy_id
    assert flipped.symbols == cfg.symbols
