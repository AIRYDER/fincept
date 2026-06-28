"""Tests for the approved-root enforcement on ``POST /models/train`` (todo 6).

The existing ``test_training.py`` covers the training store lifecycle
and the 400/429 paths.  This file focuses specifically on the
approved-root gate layered on top of ``TrainBody.input_path``:

  * An approved relative path (inside ``data/``) passes the gate and
    reaches the training orchestrator (202).
  * An absolute path outside any approved root is rejected with 422
    and ``code: "approved_roots_violation"``.
  * A traversal path (``data/../etc/passwd``) is rejected with 422.
  * An empty string is rejected with 422 and a non-empty message.

The training store is stubbed via the same ``patched_training`` fixture
pattern used in ``test_training.py``; the approved-roots gate is
monkey-patched to admit the ``tmp_path`` as a dev root so the happy-
path test can create a real input file.
"""

from __future__ import annotations

import pathlib

import pytest
from httpx import AsyncClient


@pytest.fixture
def patched_training_with_roots(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Set up the training store + approved roots for the tmp directory.

    Mirrors the ``patched_training`` fixture in ``test_training.py`` but
    kept local so this file is self-contained.  The stub trainer is a
    tiny script that always succeeds; the approved-roots gate is
    monkey-patched to admit ``tmp_path`` as an extra dev root.
    """
    import sys

    from api.training import reset_store
    from fincept_core.datasets import ApprovedRoots

    # Build a stub trainer script (same shape as test_training.py).
    script = tmp_path / "stub_trainer.py"
    script.write_text(
        """
import argparse, json, pathlib, sys

p = argparse.ArgumentParser()
p.add_argument("--input", required=True)
p.add_argument("--horizon-bars", type=int, default=15)
p.add_argument("--bar-seconds", type=int, default=60)
p.add_argument("--out-dir", required=True)
p.add_argument("--num-boost-round", type=int, default=500)
p.add_argument("--early-stopping-rounds", type=int, default=30)
p.add_argument("--cv-folds", type=int, default=0)
p.add_argument("--purge-bars", type=int, default=-1)
p.add_argument("--embargo-bars", type=int, default=0)
args = p.parse_args()

out = pathlib.Path(args.out_dir)
out.mkdir(parents=True, exist_ok=True)
(out / "model.txt").write_text("fake booster bytes")
(out / "meta.json").write_text(json.dumps({
    "features": ["a", "b"],
    "horizon_bars": args.horizon_bars,
    "bar_seconds": args.bar_seconds,
    "trained_at": 1700000000,
    "eval_mode": "walk_forward" if args.cv_folds > 0 else "holdout_80_20",
}))
print("done")
"""
    )

    runs_dir = tmp_path / "training_runs"
    models_dir = tmp_path / "models"
    runs_dir.mkdir()
    models_dir.mkdir()

    store = reset_store(
        runs_dir=runs_dir,
        models_dir=models_dir,
        max_concurrent=1,
        trainer_cmd=[sys.executable, str(script)],
    )

    # Admit tmp_path as an extra dev root.  Patch both the route-level
    # gate and the store-level gate so direct store calls also work.
    approved = ApprovedRoots(roots=[], extra_dev_roots=[tmp_path])
    monkeypatch.setattr("api.routes.models._get_approved_roots", lambda: approved)
    monkeypatch.setattr("api.training.default_approved_roots", lambda: approved)

    yield {"store": store, "tmp_path": tmp_path}


def _make_input_file(tmp_path: pathlib.Path) -> pathlib.Path:
    """Create a placeholder parquet file the validator will accept."""
    p = tmp_path / "data.parquet"
    p.write_bytes(b"")
    return p


# --------------------------------------------------------------------------- #
# Auth                                                                        #
# --------------------------------------------------------------------------- #


async def test_train_requires_auth(client: AsyncClient) -> None:
    r = await client.post(
        "/models/train",
        json={"model_name": "m", "input_path": "data/x.parquet"},
    )
    assert r.status_code == 401


# --------------------------------------------------------------------------- #
# Approved-root gate                                                          #
# --------------------------------------------------------------------------- #


async def test_train_accepts_approved_path(
    client: AsyncClient,
    auth_headers: dict[str, str],
    patched_training_with_roots,
) -> None:
    """A path inside the approved (tmp) root passes the gate → 202."""
    input_path = _make_input_file(patched_training_with_roots["tmp_path"])
    r = await client.post(
        "/models/train",
        headers=auth_headers,
        json={
            "model_name": "m_happy",
            "input_path": str(input_path),
            "cv_folds": 0,
            "num_boost_round": 5,
            "early_stopping_rounds": 5,
        },
    )
    assert r.status_code == 202
    body = r.json()
    assert body["status"] in ("queued", "running")
    assert body["request"]["model_name"] == "m_happy"


async def test_train_rejects_absolute_outside_root(
    client: AsyncClient,
    auth_headers: dict[str, str],
    patched_training_with_roots,
) -> None:
    """An absolute path outside any approved root → 422 + approved_roots_violation."""
    r = await client.post(
        "/models/train",
        headers=auth_headers,
        json={
            "model_name": "m",
            "input_path": "/etc/passwd",
        },
    )
    assert r.status_code == 422
    body = r.json()
    assert body["code"] == "approved_roots_violation"
    assert "detail" in body


async def test_train_rejects_traversal(
    client: AsyncClient,
    auth_headers: dict[str, str],
    patched_training_with_roots,
) -> None:
    """A traversal path (``data/../etc/passwd``) → 422 + approved_roots_violation."""
    r = await client.post(
        "/models/train",
        headers=auth_headers,
        json={
            "model_name": "m",
            "input_path": "data/../etc/passwd",
        },
    )
    assert r.status_code == 422
    body = r.json()
    assert body["code"] == "approved_roots_violation"


async def test_train_rejects_empty_string(
    client: AsyncClient,
    auth_headers: dict[str, str],
    patched_training_with_roots,
) -> None:
    """An empty input_path → 422 with a non-empty message (not 400)."""
    r = await client.post(
        "/models/train",
        headers=auth_headers,
        json={
            "model_name": "m",
            "input_path": "",
        },
    )
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "non-empty" in detail.lower() or "must" in detail.lower()
