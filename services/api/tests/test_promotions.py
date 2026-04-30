"""Tests for ``api.promotions`` and the ``/models/promote/*`` routes.

Layered the same way as ``test_training.py``:

  1. The ``PromotionStore`` directly -- pure filesystem behaviour with
     ``tmp_path`` isolation.  Validates atomic writes, history append,
     rollback semantics, and the resolver helper used by the agent.

  2. The HTTP routes via ``client``, asserting status codes and
     payload shapes.  The store is reset per test to a tmp dir so
     promotions don't leak across tests.

The agent-side ``_resolve_model_dir`` helper is duplicated from
``api.promotions.resolve_active_model_dir`` for runtime reasons (the
agent process avoids depending on the api package).  We test
``api.promotions.resolve_active_model_dir`` here, and the agent's copy
is checked separately in ``services/agents/tests/test_gbm_resolve.py``.
"""

from __future__ import annotations

import json
import pathlib

import pytest
from httpx import AsyncClient


# --------------------------------------------------------------------------- #
# Fixtures                                                                   #
# --------------------------------------------------------------------------- #


@pytest.fixture
def patched_promotions(tmp_path: pathlib.Path):
    """Reset the promotion-store singleton against tmp dirs.

    Returns a dict of paths so tests can plant fixture model dirs
    (under ``models_dir``) and inspect what the store wrote.
    """
    from api.promotions import reset_promotion_store

    models_dir = tmp_path / "models"
    active_dir = models_dir / "active"
    models_dir.mkdir()
    active_dir.mkdir()

    store = reset_promotion_store(
        models_dir=models_dir, active_dir=active_dir
    )
    yield {
        "models_dir": models_dir,
        "active_dir": active_dir,
        "store": store,
    }


def _write_fake_model(
    models_dir: pathlib.Path, name: str, *, missing: str | None = None
) -> pathlib.Path:
    """Plant a fake model directory the promotion validator will accept.

    ``missing`` lets us simulate a torn install: pass ``"model.txt"``
    or ``"meta.json"`` to leave that file out so we can assert the
    validator's failure modes.
    """
    d = models_dir / name
    d.mkdir(parents=True, exist_ok=True)
    if missing != "model.txt":
        (d / "model.txt").write_text("fake booster bytes")
    if missing != "meta.json":
        (d / "meta.json").write_text(
            json.dumps(
                {
                    "features": ["a", "b"],
                    "horizon_bars": 15,
                    "bar_seconds": 60,
                    "trained_at": 1700000000,
                    "eval_mode": "walk_forward",
                }
            )
        )
    return d


# --------------------------------------------------------------------------- #
# Validation                                                                  #
# --------------------------------------------------------------------------- #


class TestValidation:
    @pytest.mark.parametrize(
        "name", ["foo/bar", "..", ".secret", "a:b", "x*y", 'q"r', ""]
    )
    def test_rejects_dangerous_model_names(
        self, name: str, patched_promotions
    ) -> None:
        from api.promotions import PromotionError

        store = patched_promotions["store"]
        with pytest.raises(PromotionError):
            store.promote(agent_id="gbm_predictor.v1", model_name=name)

    @pytest.mark.parametrize("agent_id", ["foo/bar", "..", ".x", ""])
    def test_rejects_dangerous_agent_ids(
        self, agent_id: str, patched_promotions
    ) -> None:
        from api.promotions import PromotionError

        store = patched_promotions["store"]
        with pytest.raises(PromotionError):
            store.get_active(agent_id)

    def test_rejects_missing_model_dir(self, patched_promotions) -> None:
        from api.promotions import PromotionError

        store = patched_promotions["store"]
        with pytest.raises(PromotionError, match="not found"):
            store.promote(
                agent_id="gbm_predictor.v1", model_name="doesnt_exist"
            )

    def test_rejects_model_without_booster(self, patched_promotions) -> None:
        from api.promotions import PromotionError

        store = patched_promotions["store"]
        _write_fake_model(
            patched_promotions["models_dir"], "torn", missing="model.txt"
        )
        with pytest.raises(PromotionError, match="model.txt missing"):
            store.promote(agent_id="gbm_predictor.v1", model_name="torn")

    def test_rejects_model_without_meta(self, patched_promotions) -> None:
        from api.promotions import PromotionError

        store = patched_promotions["store"]
        _write_fake_model(
            patched_promotions["models_dir"], "torn", missing="meta.json"
        )
        with pytest.raises(PromotionError, match="meta.json missing"):
            store.promote(agent_id="gbm_predictor.v1", model_name="torn")


# --------------------------------------------------------------------------- #
# Store lifecycle                                                            #
# --------------------------------------------------------------------------- #


class TestStoreLifecycle:
    def test_initial_state_is_empty(self, patched_promotions) -> None:
        store = patched_promotions["store"]
        assert store.get_active("gbm_predictor.v1") is None
        assert store.get_history("gbm_predictor.v1") == []

    def test_promote_writes_pointer_and_history(
        self, patched_promotions
    ) -> None:
        store = patched_promotions["store"]
        _write_fake_model(patched_promotions["models_dir"], "m1")

        binding = store.promote(
            agent_id="gbm_predictor.v1", model_name="m1", promoted_by="alice"
        )
        assert binding.model_name == "m1"
        assert binding.promoted_by == "alice"
        assert binding.agent_id == "gbm_predictor.v1"

        # Pointer file written.
        pointer = patched_promotions["active_dir"] / "gbm_predictor.v1.json"
        assert pointer.is_file()
        data = json.loads(pointer.read_text())
        assert data["model_name"] == "m1"

        # History has one entry.
        history = store.get_history("gbm_predictor.v1")
        assert len(history) == 1
        assert history[0].model_name == "m1"

    def test_promote_twice_appends_history(self, patched_promotions) -> None:
        store = patched_promotions["store"]
        _write_fake_model(patched_promotions["models_dir"], "m1")
        _write_fake_model(patched_promotions["models_dir"], "m2")

        store.promote(agent_id="gbm_predictor.v1", model_name="m1")
        store.promote(agent_id="gbm_predictor.v1", model_name="m2")

        active = store.get_active("gbm_predictor.v1")
        assert active is not None
        assert active.model_name == "m2"

        # Newest first.
        history = store.get_history("gbm_predictor.v1")
        assert [h.model_name for h in history] == ["m2", "m1"]

    def test_history_limit_respected(self, patched_promotions) -> None:
        store = patched_promotions["store"]
        for i in range(5):
            _write_fake_model(patched_promotions["models_dir"], f"m{i}")
            store.promote(agent_id="gbm_predictor.v1", model_name=f"m{i}")

        history = store.get_history("gbm_predictor.v1", limit=3)
        assert len(history) == 3
        assert [h.model_name for h in history] == ["m4", "m3", "m2"]

    def test_history_skips_malformed_lines(self, patched_promotions) -> None:
        """Hand-edited history shouldn't break the listing."""
        store = patched_promotions["store"]
        _write_fake_model(patched_promotions["models_dir"], "m1")
        store.promote(agent_id="gbm_predictor.v1", model_name="m1")

        history_path = (
            patched_promotions["active_dir"] / "gbm_predictor.v1.history.jsonl"
        )
        # Append a malformed line + a blank line + a valid one.
        with history_path.open("a") as f:
            f.write("not json\n")
            f.write("\n")
            f.write(
                json.dumps(
                    {
                        "agent_id": "gbm_predictor.v1",
                        "model_name": "manually_added",
                        "promoted_at": 123.0,
                        "promoted_by": "vim",
                    }
                )
                + "\n"
            )

        history = store.get_history("gbm_predictor.v1")
        names = [h.model_name for h in history]
        assert "manually_added" in names
        assert "m1" in names
        # Garbage line wasn't counted.
        assert len(history) == 2

    def test_get_active_returns_none_on_corrupt_pointer(
        self, patched_promotions
    ) -> None:
        """A hand-edited active.json shouldn't crash the listing UI."""
        store = patched_promotions["store"]
        pointer = patched_promotions["active_dir"] / "gbm_predictor.v1.json"
        pointer.write_text("{not json")
        assert store.get_active("gbm_predictor.v1") is None

    def test_atomic_write_no_partial_file(self, patched_promotions) -> None:
        """The .tmp file shouldn't survive after a successful promote."""
        store = patched_promotions["store"]
        _write_fake_model(patched_promotions["models_dir"], "m1")
        store.promote(agent_id="gbm_predictor.v1", model_name="m1")

        leftovers = list(patched_promotions["active_dir"].glob("*.tmp"))
        assert leftovers == []


# --------------------------------------------------------------------------- #
# Rollback semantics                                                         #
# --------------------------------------------------------------------------- #


class TestRollback:
    def test_rollback_with_no_history_returns_none(
        self, patched_promotions
    ) -> None:
        store = patched_promotions["store"]
        result = store.rollback(agent_id="gbm_predictor.v1")
        assert result is None
        # Pointer doesn't get created out of nothing.
        pointer = patched_promotions["active_dir"] / "gbm_predictor.v1.json"
        assert not pointer.exists()

    def test_rollback_with_one_history_clears_active(
        self, patched_promotions
    ) -> None:
        store = patched_promotions["store"]
        _write_fake_model(patched_promotions["models_dir"], "m1")
        store.promote(agent_id="gbm_predictor.v1", model_name="m1")

        result = store.rollback(agent_id="gbm_predictor.v1")
        assert result is None

        # Active pointer is gone.
        pointer = patched_promotions["active_dir"] / "gbm_predictor.v1.json"
        assert not pointer.exists()
        assert store.get_active("gbm_predictor.v1") is None

        # History records the clear with a sentinel name.
        history = store.get_history("gbm_predictor.v1")
        assert history[0].model_name == "(rolled-back-to-empty)"

    def test_rollback_restores_previous(self, patched_promotions) -> None:
        store = patched_promotions["store"]
        _write_fake_model(patched_promotions["models_dir"], "m1")
        _write_fake_model(patched_promotions["models_dir"], "m2")

        store.promote(agent_id="gbm_predictor.v1", model_name="m1")
        store.promote(agent_id="gbm_predictor.v1", model_name="m2")

        rolled = store.rollback(
            agent_id="gbm_predictor.v1", promoted_by="alice"
        )
        assert rolled is not None
        assert rolled.model_name == "m1"
        # Tagged so the audit trail records the rollback.
        assert "rollback" in rolled.promoted_by

        # Active pointer reflects the rollback.
        active = store.get_active("gbm_predictor.v1")
        assert active is not None
        assert active.model_name == "m1"

        # History is now [m1-rollback, m2, m1] -- the rollback itself
        # was appended.
        history = store.get_history("gbm_predictor.v1", limit=10)
        assert [h.model_name for h in history] == ["m1", "m2", "m1"]

    def test_rollback_revalidates_target(self, patched_promotions) -> None:
        """If the previous model has been deleted, rollback fails cleanly
        rather than promoting a missing directory."""
        from api.promotions import PromotionError

        store = patched_promotions["store"]
        _write_fake_model(patched_promotions["models_dir"], "m1")
        _write_fake_model(patched_promotions["models_dir"], "m2")
        store.promote(agent_id="gbm_predictor.v1", model_name="m1")
        store.promote(agent_id="gbm_predictor.v1", model_name="m2")

        # Delete the m1 directory (operator's mistake).
        import shutil

        shutil.rmtree(patched_promotions["models_dir"] / "m1")

        with pytest.raises(PromotionError, match="not found"):
            store.rollback(agent_id="gbm_predictor.v1")


# --------------------------------------------------------------------------- #
# resolve_active_model_dir helper                                            #
# --------------------------------------------------------------------------- #


class TestResolveActiveModelDir:
    def test_falls_back_to_default_when_no_active(
        self, patched_promotions, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No pointer + no env var -> ``models/<default_model>``."""
        from api.promotions import resolve_active_model_dir

        monkeypatch.setenv(
            "ACTIVE_MODELS_DIR", str(patched_promotions["active_dir"])
        )
        monkeypatch.delenv("GBM_MODEL_DIR", raising=False)

        resolved = resolve_active_model_dir(
            "gbm_predictor.v1",
            models_dir=patched_promotions["models_dir"],
            env_fallback="GBM_MODEL_DIR",
        )
        assert resolved == patched_promotions["models_dir"] / "gbm_predictor"

    def test_uses_env_when_no_active(
        self, patched_promotions, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from api.promotions import resolve_active_model_dir

        monkeypatch.setenv(
            "ACTIVE_MODELS_DIR", str(patched_promotions["active_dir"])
        )
        custom = tmp_path / "envar_choice"
        monkeypatch.setenv("GBM_MODEL_DIR", str(custom))

        resolved = resolve_active_model_dir(
            "gbm_predictor.v1",
            models_dir=patched_promotions["models_dir"],
            env_fallback="GBM_MODEL_DIR",
        )
        assert resolved == custom

    def test_active_pointer_wins(
        self, patched_promotions, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Even with env var set, the active pointer takes precedence."""
        from api.promotions import resolve_active_model_dir

        monkeypatch.setenv(
            "ACTIVE_MODELS_DIR", str(patched_promotions["active_dir"])
        )
        monkeypatch.setenv("GBM_MODEL_DIR", "models/should_be_ignored")

        store = patched_promotions["store"]
        _write_fake_model(patched_promotions["models_dir"], "promoted_one")
        store.promote(
            agent_id="gbm_predictor.v1", model_name="promoted_one"
        )

        resolved = resolve_active_model_dir(
            "gbm_predictor.v1",
            models_dir=patched_promotions["models_dir"],
            env_fallback="GBM_MODEL_DIR",
        )
        assert resolved == patched_promotions["models_dir"] / "promoted_one"

    def test_corrupt_pointer_falls_through(
        self, patched_promotions, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from api.promotions import resolve_active_model_dir

        monkeypatch.setenv(
            "ACTIVE_MODELS_DIR", str(patched_promotions["active_dir"])
        )
        monkeypatch.delenv("GBM_MODEL_DIR", raising=False)

        pointer = patched_promotions["active_dir"] / "gbm_predictor.v1.json"
        pointer.write_text("{not json")

        resolved = resolve_active_model_dir(
            "gbm_predictor.v1",
            models_dir=patched_promotions["models_dir"],
            env_fallback="GBM_MODEL_DIR",
        )
        # Falls through to default.
        assert resolved == patched_promotions["models_dir"] / "gbm_predictor"


# --------------------------------------------------------------------------- #
# Routes                                                                     #
# --------------------------------------------------------------------------- #


class TestPromoteRoutes:
    @pytest.mark.asyncio
    async def test_get_active_requires_auth(
        self, client: AsyncClient, patched_promotions
    ) -> None:
        response = await client.get("/models/promote/active")
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_post_promote_requires_auth(
        self, client: AsyncClient, patched_promotions
    ) -> None:
        response = await client.post("/models/anything/promote", json={})
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_rollback_requires_auth(
        self, client: AsyncClient, patched_promotions
    ) -> None:
        response = await client.post("/models/promote/rollback", json={})
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_active_when_empty(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        patched_promotions,
    ) -> None:
        response = await client.get(
            "/models/promote/active", headers=auth_headers
        )
        assert response.status_code == 200
        body = response.json()
        assert body["agent_id"] == "gbm_predictor.v1"
        assert body["active"] is None
        assert body["history"] == []

    @pytest.mark.asyncio
    async def test_promote_then_query_active(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        patched_promotions,
    ) -> None:
        # MODELS_DIR isn't patched on the api process, so we have to
        # plant the fake model under the same MODELS_DIR the api will
        # see.  The promotions store was reset to point at our tmp
        # models_dir, but the route's `_resolve_model_dir` reads
        # MODELS_DIR (env-resolved at import time).  Use the env var.
        import os

        os.environ["MODELS_DIR"] = str(patched_promotions["models_dir"])
        # The detail-route side reads MODELS_DIR at module top-level
        # so patching env here only works when the test conftest
        # already routed it; verify by importing the route module and
        # rewriting its captured constant.
        import api.routes.models as models_route

        models_route._MODELS_DIR = patched_promotions["models_dir"]

        _write_fake_model(patched_promotions["models_dir"], "m_first")

        response = await client.post(
            "/models/m_first/promote",
            headers=auth_headers,
            json={"agent_id": "gbm_predictor.v1", "promoted_by": "tester"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["active"]["model_name"] == "m_first"
        assert body["restart_required"] is True

        # Now query active state.
        active = await client.get(
            "/models/promote/active", headers=auth_headers
        )
        assert active.json()["active"]["model_name"] == "m_first"

    @pytest.mark.asyncio
    async def test_promote_400_on_missing_model(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        patched_promotions,
    ) -> None:
        import api.routes.models as models_route

        models_route._MODELS_DIR = patched_promotions["models_dir"]

        # Don't plant any model dir; the resolver returns 404 before
        # the promotion store ever runs.
        response = await client.post(
            "/models/no_such/promote",
            headers=auth_headers,
            json={},
        )
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_promote_400_on_invalid_name(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        patched_promotions,
    ) -> None:
        import api.routes.models as models_route

        models_route._MODELS_DIR = patched_promotions["models_dir"]

        # Path-traversal name should hit the 400 branch in
        # _resolve_model_dir.
        response = await client.post(
            "/models/..secret/promote",
            headers=auth_headers,
            json={},
        )
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_rollback_route_round_trip(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        patched_promotions,
    ) -> None:
        import api.routes.models as models_route

        models_route._MODELS_DIR = patched_promotions["models_dir"]

        _write_fake_model(patched_promotions["models_dir"], "first")
        _write_fake_model(patched_promotions["models_dir"], "second")

        await client.post(
            "/models/first/promote",
            headers=auth_headers,
            json={"agent_id": "gbm_predictor.v1"},
        )
        await client.post(
            "/models/second/promote",
            headers=auth_headers,
            json={"agent_id": "gbm_predictor.v1"},
        )

        # Active is currently 'second'.
        active = await client.get(
            "/models/promote/active", headers=auth_headers
        )
        assert active.json()["active"]["model_name"] == "second"

        rollback = await client.post(
            "/models/promote/rollback",
            headers=auth_headers,
            json={"agent_id": "gbm_predictor.v1"},
        )
        assert rollback.status_code == 200
        assert rollback.json()["active"]["model_name"] == "first"

    @pytest.mark.asyncio
    async def test_active_history_limit_validation(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        patched_promotions,
    ) -> None:
        response = await client.get(
            "/models/promote/active?history_limit=0", headers=auth_headers
        )
        assert response.status_code == 400
        response = await client.get(
            "/models/promote/active?history_limit=999", headers=auth_headers
        )
        assert response.status_code == 400


# --------------------------------------------------------------------------- #
# Shadow slot (Phase E1)                                                     #
# --------------------------------------------------------------------------- #


class TestShadowStore:
    """Direct PromotionStore tests for the shadow methods."""

    def test_get_shadow_returns_none_when_unset(
        self, patched_promotions
    ) -> None:
        store = patched_promotions["store"]
        assert store.get_shadow("gbm_predictor.v1") is None

    def test_set_shadow_round_trip(self, patched_promotions) -> None:
        store = patched_promotions["store"]
        _write_fake_model(patched_promotions["models_dir"], "candidate")

        binding = store.set_shadow(
            agent_id="gbm_predictor.v1",
            model_name="candidate",
            promoted_by="tester",
        )
        assert binding.model_name == "candidate"
        assert binding.promoted_by == "tester"
        # Read it back.
        again = store.get_shadow("gbm_predictor.v1")
        assert again is not None
        assert again.model_name == "candidate"

    def test_set_shadow_writes_separate_file_from_active(
        self, patched_promotions
    ) -> None:
        """Setting shadow must not touch the active pointer file."""
        store = patched_promotions["store"]
        _write_fake_model(patched_promotions["models_dir"], "active_one")
        _write_fake_model(patched_promotions["models_dir"], "shadow_one")
        store.promote(agent_id="gbm_predictor.v1", model_name="active_one")
        store.set_shadow(
            agent_id="gbm_predictor.v1", model_name="shadow_one"
        )
        # Both pointers exist, with different files.
        active_path = (
            patched_promotions["active_dir"] / "gbm_predictor.v1.json"
        )
        shadow_path = (
            patched_promotions["active_dir"] / "gbm_predictor.v1.shadow.json"
        )
        assert active_path.is_file()
        assert shadow_path.is_file()
        # And the active binding still says active_one.
        active = store.get_active("gbm_predictor.v1")
        assert active is not None
        assert active.model_name == "active_one"

    def test_set_shadow_rejects_missing_model_dir(
        self, patched_promotions
    ) -> None:
        from api.promotions import PromotionError

        store = patched_promotions["store"]
        with pytest.raises(PromotionError, match="model directory not found"):
            store.set_shadow(
                agent_id="gbm_predictor.v1", model_name="nope"
            )

    def test_set_shadow_rejects_missing_model_txt(
        self, patched_promotions
    ) -> None:
        from api.promotions import PromotionError

        store = patched_promotions["store"]
        _write_fake_model(
            patched_promotions["models_dir"], "torn", missing="model.txt"
        )
        with pytest.raises(PromotionError, match="model.txt missing"):
            store.set_shadow(
                agent_id="gbm_predictor.v1", model_name="torn"
            )

    def test_set_shadow_rejects_missing_meta_json(
        self, patched_promotions
    ) -> None:
        from api.promotions import PromotionError

        store = patched_promotions["store"]
        _write_fake_model(
            patched_promotions["models_dir"], "torn", missing="meta.json"
        )
        with pytest.raises(PromotionError, match="meta.json missing"):
            store.set_shadow(
                agent_id="gbm_predictor.v1", model_name="torn"
            )

    @pytest.mark.parametrize("bad", ["", "..", ".secret", "a/b"])
    def test_set_shadow_rejects_bad_model_name(
        self, patched_promotions, bad: str
    ) -> None:
        from api.promotions import PromotionError

        store = patched_promotions["store"]
        with pytest.raises(PromotionError):
            store.set_shadow(
                agent_id="gbm_predictor.v1", model_name=bad
            )

    def test_set_shadow_rejects_when_equal_to_active(
        self, patched_promotions
    ) -> None:
        from api.promotions import PromotionError

        store = patched_promotions["store"]
        _write_fake_model(patched_promotions["models_dir"], "same")
        store.promote(agent_id="gbm_predictor.v1", model_name="same")
        with pytest.raises(PromotionError, match="already active"):
            store.set_shadow(
                agent_id="gbm_predictor.v1", model_name="same"
            )

    def test_clear_shadow_returns_true_when_present(
        self, patched_promotions
    ) -> None:
        store = patched_promotions["store"]
        _write_fake_model(patched_promotions["models_dir"], "candidate")
        store.set_shadow(
            agent_id="gbm_predictor.v1", model_name="candidate"
        )
        assert store.clear_shadow("gbm_predictor.v1") is True
        assert store.get_shadow("gbm_predictor.v1") is None

    def test_clear_shadow_idempotent_when_absent(
        self, patched_promotions
    ) -> None:
        store = patched_promotions["store"]
        # Never set; clearing should return False, not raise.
        assert store.clear_shadow("gbm_predictor.v1") is False
        # Second call also False.
        assert store.clear_shadow("gbm_predictor.v1") is False

    def test_promote_does_not_touch_shadow(self, patched_promotions) -> None:
        """Promoting active must leave shadow alone."""
        store = patched_promotions["store"]
        _write_fake_model(patched_promotions["models_dir"], "active_one")
        _write_fake_model(patched_promotions["models_dir"], "shadow_one")
        store.set_shadow(
            agent_id="gbm_predictor.v1", model_name="shadow_one"
        )
        store.promote(agent_id="gbm_predictor.v1", model_name="active_one")
        # Shadow still set.
        shadow = store.get_shadow("gbm_predictor.v1")
        assert shadow is not None
        assert shadow.model_name == "shadow_one"

    def test_rollback_does_not_touch_shadow(self, patched_promotions) -> None:
        """Active rollback must leave shadow alone."""
        store = patched_promotions["store"]
        _write_fake_model(patched_promotions["models_dir"], "first")
        _write_fake_model(patched_promotions["models_dir"], "second")
        _write_fake_model(patched_promotions["models_dir"], "shadow_one")
        store.promote(agent_id="gbm_predictor.v1", model_name="first")
        store.promote(agent_id="gbm_predictor.v1", model_name="second")
        store.set_shadow(
            agent_id="gbm_predictor.v1", model_name="shadow_one"
        )
        store.rollback(agent_id="gbm_predictor.v1")
        # Shadow still set.
        shadow = store.get_shadow("gbm_predictor.v1")
        assert shadow is not None
        assert shadow.model_name == "shadow_one"


class TestShadowRoutes:
    """HTTP-layer tests for /{name}/shadow + /promote/shadow/clear."""

    @pytest.mark.asyncio
    async def test_post_shadow_requires_auth(
        self, client: AsyncClient, patched_promotions
    ) -> None:
        response = await client.post("/models/anything/shadow", json={})
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_clear_shadow_requires_auth(
        self, client: AsyncClient, patched_promotions
    ) -> None:
        response = await client.post(
            "/models/promote/shadow/clear", json={}
        )
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_active_endpoint_returns_shadow_field(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        patched_promotions,
    ) -> None:
        """The augmented GET /promote/active must include a ``shadow`` key."""
        response = await client.get(
            "/models/promote/active", headers=auth_headers
        )
        body = response.json()
        assert "shadow" in body
        assert body["shadow"] is None

    @pytest.mark.asyncio
    async def test_set_shadow_round_trip_via_http(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        patched_promotions,
    ) -> None:
        import api.routes.models as models_route

        models_route._MODELS_DIR = patched_promotions["models_dir"]
        _write_fake_model(patched_promotions["models_dir"], "candidate")

        # Set shadow via POST.
        post = await client.post(
            "/models/candidate/shadow",
            headers=auth_headers,
            json={"agent_id": "gbm_predictor.v1", "promoted_by": "tester"},
        )
        assert post.status_code == 200
        body = post.json()
        assert body["shadow"]["model_name"] == "candidate"
        assert body["active"] is None  # never promoted active

        # GET /promote/active returns the same shadow.
        active = await client.get(
            "/models/promote/active", headers=auth_headers
        )
        assert active.json()["shadow"]["model_name"] == "candidate"

    @pytest.mark.asyncio
    async def test_set_shadow_404_on_missing_model(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        patched_promotions,
    ) -> None:
        import api.routes.models as models_route

        models_route._MODELS_DIR = patched_promotions["models_dir"]
        response = await client.post(
            "/models/no_such/shadow",
            headers=auth_headers,
            json={},
        )
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_set_shadow_400_when_equal_to_active(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        patched_promotions,
    ) -> None:
        import api.routes.models as models_route

        models_route._MODELS_DIR = patched_promotions["models_dir"]
        _write_fake_model(patched_promotions["models_dir"], "live")
        await client.post(
            "/models/live/promote",
            headers=auth_headers,
            json={"agent_id": "gbm_predictor.v1"},
        )
        # Now try to shadow the same model.
        response = await client.post(
            "/models/live/shadow",
            headers=auth_headers,
            json={"agent_id": "gbm_predictor.v1"},
        )
        assert response.status_code == 400
        assert "already active" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_clear_shadow_round_trip(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        patched_promotions,
    ) -> None:
        import api.routes.models as models_route

        models_route._MODELS_DIR = patched_promotions["models_dir"]
        _write_fake_model(patched_promotions["models_dir"], "candidate")
        await client.post(
            "/models/candidate/shadow",
            headers=auth_headers,
            json={"agent_id": "gbm_predictor.v1"},
        )

        clear = await client.post(
            "/models/promote/shadow/clear",
            headers=auth_headers,
            json={"agent_id": "gbm_predictor.v1"},
        )
        assert clear.status_code == 200
        body = clear.json()
        assert body["cleared"] is True
        assert body["shadow"] is None

        # And /promote/active reflects it.
        active = await client.get(
            "/models/promote/active", headers=auth_headers
        )
        assert active.json()["shadow"] is None

    @pytest.mark.asyncio
    async def test_clear_shadow_idempotent_via_http(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        patched_promotions,
    ) -> None:
        """Clearing an already-empty shadow returns 200 with cleared=False."""
        response = await client.post(
            "/models/promote/shadow/clear",
            headers=auth_headers,
            json={"agent_id": "gbm_predictor.v1"},
        )
        assert response.status_code == 200
        assert response.json()["cleared"] is False
