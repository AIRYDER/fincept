"""Tests for ``api.training`` and the ``/models/train`` + ``/models/runs`` routes.

Strategy
~~~~~~~~

Subprocess management is the riskiest part of this module so we test
two layers:

1. The store / dataclasses / persistence directly with a stubbed
   trainer command (a tiny standalone script that exits 0 / non-zero
   on demand).  This exercises the lifecycle without depending on
   lightgbm, polars, or anything in ``agents/``.

2. The HTTP routes via the ``client`` fixture, asserting status codes
   and payload shapes.  The actual subprocess is again stubbed via
   constructor injection so the test stays fast and hermetic.

We do *not* test against the real trainer here -- that's the job of
``services/agents/tests/test_walk_forward_cv.py`` and friends.  The
boundary we care about is "given a request, the api spawns a process,
captures its output, and reports a terminal status".

Why constructor injection rather than env vars?
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Because env vars + module-level constants would have forced
``importlib.reload(api.training)`` in every test, and ``shlex.split``
of a Windows path inside ``MODEL_TRAINER_CMD`` strips backslashes
(POSIX mode is the default).  Both bugs went away once the store
took its config as plain ``__init__`` arguments.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import sys
from typing import Any

import pytest
from httpx import AsyncClient


# --------------------------------------------------------------------------- #
# Fixtures                                                                   #
# --------------------------------------------------------------------------- #


@pytest.fixture
def stub_trainer_cmd(tmp_path: pathlib.Path) -> list[str]:
    """Build a tiny Python script that mimics the trainer's CLI shape.

    Behaviour controlled by ``--input``:

      * 'fail' in input  -> exit 1, stderr message captured to log.
      * default          -> write a stub model.txt + meta.json, exit 0.

    Returned as a *list* of argv tokens so we never go through shlex
    (which on Windows in POSIX mode would mangle backslashed paths).
    """
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

print(f"stub trainer: input={args.input} cv_folds={args.cv_folds}")

if "fail" in args.input:
    print("simulated trainer failure", file=sys.stderr)
    sys.exit(1)

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
    return [sys.executable, str(script)]


@pytest.fixture
def patched_training(
    tmp_path: pathlib.Path,
    stub_trainer_cmd: list[str],
    monkeypatch: pytest.MonkeyPatch,
):
    """Reset the singleton store against tmp dirs + the stub trainer.

    Yields a dict of the fresh paths so tests can assert on what the
    trainer wrote.  No env mutation, no module reloads -- just plain
    constructor injection.

    Also monkey-patches ``_get_approved_roots`` so the tmp directory is
    an approved dev root -- the approved-root gate (todo 6) would
    otherwise reject the tmp_path-based input files the tests create.
    """
    from api.training import reset_store
    from fincept_core.datasets import ApprovedRoots

    runs_dir = tmp_path / "training_runs"
    models_dir = tmp_path / "models"
    runs_dir.mkdir()
    models_dir.mkdir()

    store = reset_store(
        runs_dir=runs_dir,
        models_dir=models_dir,
        max_concurrent=1,
        trainer_cmd=stub_trainer_cmd,
    )

    # Admit the tmp_path as an extra dev root so test-created input
    # files pass the approved-root gate.  The route layer reads
    # ``api.routes.models._get_approved_roots`` while the store layer
    # (``_validate_input_path``) reads ``api.training.default_approved_roots``
    # directly, so both must be patched for direct store calls to work.
    approved = ApprovedRoots(roots=[], extra_dev_roots=[tmp_path])
    monkeypatch.setattr("api.routes.models._get_approved_roots", lambda: approved)
    monkeypatch.setattr("api.training.default_approved_roots", lambda: approved)

    yield {
        "runs_dir": runs_dir,
        "models_dir": models_dir,
        "store": store,
        "tmp_path": tmp_path,
    }


def _input_parquet(
    tmp_path: pathlib.Path, *, name: str = "data.parquet"
) -> pathlib.Path:
    """Create a placeholder file the validator will accept.

    Content doesn't matter -- the stub trainer ignores it.  We just
    need ``is_file()`` to return True.
    """
    p = tmp_path / name
    p.write_bytes(b"")
    return p


def _build_request(input_path: pathlib.Path, *, model_name: str = "m") -> Any:
    from api.training import TrainingRequest

    return TrainingRequest(
        model_name=model_name,
        input_path=str(input_path),
        horizon_bars=15,
        bar_seconds=60,
        cv_folds=2,
        purge_bars=-1,
        embargo_bars=0,
        num_boost_round=10,
        early_stopping_rounds=5,
    )


async def _wait_terminal(store: Any, run_id: str, *, timeout: float = 15.0) -> Any:
    """Spin until the run leaves queued/running, or fail the test.

    15s is a generous timeout: keeps the test fast on CI while
    accepting cold Python startup on Windows (~1s) plus the stub
    trainer's negligible work.
    """
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        run = store.get(run_id)
        if run and run.status in ("completed", "failed"):
            return run
        await asyncio.sleep(0.05)
    raise AssertionError(f"run {run_id} never reached a terminal state")


# --------------------------------------------------------------------------- #
# TrainingRequest.as_cli_args                                                #
# --------------------------------------------------------------------------- #


class TestAsCliArgs:
    def _req(self, **overrides: Any):
        from api.training import TrainingRequest

        defaults: dict[str, Any] = dict(
            model_name="m",
            input_path="x.parquet",
            horizon_bars=15,
            bar_seconds=60,
            cv_folds=5,
            purge_bars=-1,
            embargo_bars=0,
            num_boost_round=500,
            early_stopping_rounds=30,
        )
        defaults.update(overrides)
        return TrainingRequest(**defaults)

    def test_includes_required_flags(self) -> None:
        req = self._req()
        argv = req.as_cli_args(out_dir=pathlib.Path("models/m"))
        assert "--input" in argv
        assert "x.parquet" in argv
        assert "--cv-folds" in argv
        assert "5" in argv
        assert "--out-dir" in argv

    def test_omits_purge_bars_when_negative(self) -> None:
        req = self._req(purge_bars=-1)
        argv = req.as_cli_args(out_dir=pathlib.Path("models/m"))
        assert "--purge-bars" not in argv

    def test_includes_purge_bars_when_set(self) -> None:
        req = self._req(purge_bars=20)
        argv = req.as_cli_args(out_dir=pathlib.Path("models/m"))
        idx = argv.index("--purge-bars")
        assert argv[idx + 1] == "20"


# --------------------------------------------------------------------------- #
# Validation                                                                 #
# --------------------------------------------------------------------------- #


class TestValidation:
    def test_rejects_empty_model_name(self, tmp_path: pathlib.Path) -> None:
        from api.training import (
            TrainingRequest,
            TrainingValidationError,
            _validate_request,
        )

        req = TrainingRequest(
            model_name="",
            input_path=str(_input_parquet(tmp_path)),
            horizon_bars=15,
            bar_seconds=60,
            cv_folds=5,
            purge_bars=-1,
            embargo_bars=0,
            num_boost_round=500,
            early_stopping_rounds=30,
        )
        with pytest.raises(TrainingValidationError):
            _validate_request(req)

    @pytest.mark.parametrize("name", ["foo/bar", "..", ".secret", "a:b", "x*y", 'q"r'])
    def test_rejects_dangerous_names(self, name: str, tmp_path: pathlib.Path) -> None:
        from api.training import (
            TrainingRequest,
            TrainingValidationError,
            _validate_request,
        )

        req = TrainingRequest(
            model_name=name,
            input_path=str(_input_parquet(tmp_path)),
            horizon_bars=15,
            bar_seconds=60,
            cv_folds=5,
            purge_bars=-1,
            embargo_bars=0,
            num_boost_round=500,
            early_stopping_rounds=30,
        )
        with pytest.raises(TrainingValidationError):
            _validate_request(req)

    def test_rejects_missing_input_file(self) -> None:
        from api.training import (
            TrainingRequest,
            TrainingValidationError,
            _validate_request,
        )

        req = TrainingRequest(
            model_name="m",
            input_path="/nonexistent/path.parquet",
            horizon_bars=15,
            bar_seconds=60,
            cv_folds=5,
            purge_bars=-1,
            embargo_bars=0,
            num_boost_round=500,
            early_stopping_rounds=30,
        )
        # The approved-roots gate rejects the path before the is_file()
        # check fires, so the error message is "rejected" not "not found".
        with pytest.raises(TrainingValidationError, match="rejected"):
            _validate_request(req)


# --------------------------------------------------------------------------- #
# Subprocess lifecycle (direct store API)                                    #
# --------------------------------------------------------------------------- #


class TestStoreLifecycle:
    @pytest.mark.asyncio
    async def test_completes_successfully(
        self, tmp_path: pathlib.Path, patched_training
    ) -> None:
        store = patched_training["store"]
        req = _build_request(_input_parquet(tmp_path))
        run = await store.start_run(req)
        assert run.status in ("queued", "running")

        terminal = await _wait_terminal(store, run.run_id)
        assert terminal.status == "completed"
        assert terminal.exit_code == 0
        assert terminal.error is None
        assert terminal.started_at is not None
        assert terminal.finished_at >= terminal.started_at
        # Subprocess wrote model artifacts to MODELS_DIR.
        out = pathlib.Path(terminal.out_dir)
        assert (out / "meta.json").is_file()
        assert (out / "model.txt").is_file()
        # Log file captured stdout.
        log = pathlib.Path(terminal.log_path).read_text()
        assert "stub trainer" in log
        assert "done" in log

    @pytest.mark.asyncio
    async def test_records_failure_on_nonzero_exit(
        self, tmp_path: pathlib.Path, patched_training
    ) -> None:
        store = patched_training["store"]
        # Stub fails when 'fail' appears in input path.
        req = _build_request(_input_parquet(tmp_path, name="fail.parquet"))
        run = await store.start_run(req)
        terminal = await _wait_terminal(store, run.run_id)
        assert terminal.status == "failed"
        assert terminal.exit_code == 1
        assert "code 1" in (terminal.error or "")
        log = pathlib.Path(terminal.log_path).read_text()
        assert "simulated trainer failure" in log

    @pytest.mark.asyncio
    async def test_concurrency_cap(
        self, tmp_path: pathlib.Path, patched_training
    ) -> None:
        """With max_concurrent=1, the second start_run while the first
        is in flight must raise the 'in flight' validation error."""
        from api.training import TrainingValidationError

        store = patched_training["store"]
        req1 = _build_request(_input_parquet(tmp_path), model_name="a")
        req2 = _build_request(
            _input_parquet(tmp_path, name="b.parquet"), model_name="b"
        )

        await store.start_run(req1)
        with pytest.raises(TrainingValidationError, match="in flight"):
            await store.start_run(req2)

        # Once the first finishes, the slot frees up.
        await _wait_terminal(store, store.list_runs()[0].run_id)
        await store.start_run(req2)
        # Drain the second to keep the test environment clean.
        await _wait_terminal(store, store.list_runs()[0].run_id)

    @pytest.mark.asyncio
    async def test_persistence_round_trip(
        self,
        tmp_path: pathlib.Path,
        patched_training,
        stub_trainer_cmd: list[str],
    ) -> None:
        """After a completed run, a fresh TrainingStore reading the same
        runs_dir must reproduce the run record."""
        from api.training import TrainingStore

        store = patched_training["store"]
        req = _build_request(_input_parquet(tmp_path))
        run = await store.start_run(req)
        await _wait_terminal(store, run.run_id)

        # New store re-reading the same dir picks up the persisted record.
        fresh = TrainingStore(
            runs_dir=patched_training["runs_dir"],
            models_dir=patched_training["models_dir"],
            max_concurrent=1,
            trainer_cmd=stub_trainer_cmd,
        )
        replayed = fresh.get(run.run_id)
        assert replayed is not None
        assert replayed.status == "completed"
        assert replayed.request.model_name == req.model_name

    def test_running_run_marked_resumable_failed_on_reload(
        self, tmp_path: pathlib.Path, patched_training, stub_trainer_cmd: list[str]
    ) -> None:
        """If a record on disk says status=running but the process is
        gone (api restarted), the new store boots it as
        'resumable_failed' so the UI doesn't show a phantom in-flight
        job forever and the operator can re-launch it."""
        from api.training import TrainingStore

        runs_dir = patched_training["runs_dir"]
        record = runs_dir / "stale.json"
        record.write_text(
            json.dumps(
                {
                    "run_id": "stale",
                    "status": "running",
                    "created_at": 1.0,
                    "started_at": 1.0,
                    "finished_at": None,
                    "exit_code": None,
                    "out_dir": str(tmp_path / "models" / "m"),
                    "log_path": str(runs_dir / "stale.log"),
                    "request": {
                        "model_name": "m",
                        "input_path": "x.parquet",
                        "horizon_bars": 15,
                        "bar_seconds": 60,
                        "cv_folds": 5,
                        "purge_bars": -1,
                        "embargo_bars": 0,
                        "num_boost_round": 500,
                        "early_stopping_rounds": 30,
                    },
                }
            )
        )
        fresh = TrainingStore(
            runs_dir=runs_dir,
            models_dir=patched_training["models_dir"],
            trainer_cmd=stub_trainer_cmd,
        )
        run = fresh.get("stale")
        assert run is not None
        assert run.status == "resumable_failed"
        assert "api restarted" in (run.error or "")
        assert "resumable" in (run.error or "")

    def test_malformed_record_dropped(
        self, patched_training, stub_trainer_cmd: list[str]
    ) -> None:
        from api.training import TrainingStore

        runs_dir = patched_training["runs_dir"]
        (runs_dir / "broken.json").write_text("{not json")
        fresh = TrainingStore(
            runs_dir=runs_dir,
            models_dir=patched_training["models_dir"],
            trainer_cmd=stub_trainer_cmd,
        )
        # Did not crash, did not appear in the listing.
        assert all(r.run_id != "broken" for r in fresh.list_runs())

    @pytest.mark.asyncio
    async def test_resume_run_relaunches_resumable_failed(
        self, tmp_path: pathlib.Path, patched_training, stub_trainer_cmd: list[str]
    ) -> None:
        """A resumable_failed run can be re-launched via resume_run; it
        returns to queued and eventually completes."""
        from api.training import TrainingStore

        runs_dir = patched_training["runs_dir"]
        models_dir = patched_training["models_dir"]
        record = runs_dir / "resumable.json"
        record.write_text(
            json.dumps(
                {
                    "run_id": "resumable",
                    "status": "resumable_failed",
                    "created_at": 1.0,
                    "started_at": 1.0,
                    "finished_at": 2.0,
                    "exit_code": None,
                    "out_dir": str(models_dir / "m"),
                    "log_path": str(runs_dir / "resumable.log"),
                    "request": {
                        "model_name": "m",
                        "input_path": str(_input_parquet(tmp_path)),
                        "horizon_bars": 15,
                        "bar_seconds": 60,
                        "cv_folds": 0,
                        "purge_bars": -1,
                        "embargo_bars": 0,
                        "num_boost_round": 5,
                        "early_stopping_rounds": 5,
                    },
                }
            )
        )
        fresh = TrainingStore(
            runs_dir=runs_dir,
            models_dir=models_dir,
            max_concurrent=1,
            trainer_cmd=stub_trainer_cmd,
        )
        run = fresh.get("resumable")
        assert run is not None
        assert run.status == "resumable_failed"

        resumed = await fresh.resume_run("resumable")
        assert resumed.status == "queued"
        assert resumed.resume_token is not None
        assert resumed.resume_token.startswith("resume-")
        assert resumed.started_at is None
        assert resumed.finished_at is None
        assert resumed.error is None

        terminal = await _wait_terminal(fresh, "resumable")
        assert terminal.status == "completed"
        assert terminal.exit_code == 0

    @pytest.mark.asyncio
    async def test_resume_run_rejects_non_resumable(
        self, tmp_path: pathlib.Path, patched_training
    ) -> None:
        """resume_run raises TrainingValidationError for a run that is
        not in the resumable_failed state."""
        from api.training import TrainingValidationError

        store = patched_training["store"]
        req = _build_request(_input_parquet(tmp_path))
        run = await store.start_run(req)
        await _wait_terminal(store, run.run_id)
        # Run is now 'completed' -- not resumable.
        with pytest.raises(TrainingValidationError, match="not resumable"):
            await store.resume_run(run.run_id)

    @pytest.mark.asyncio
    async def test_resume_run_rejects_unknown_run(self, patched_training) -> None:
        """resume_run raises TrainingValidationError for a missing run."""
        from api.training import TrainingValidationError

        store = patched_training["store"]
        with pytest.raises(TrainingValidationError, match="not found"):
            await store.resume_run("does-not-exist")

    def test_heartbeat_updates_heartbeat_at(
        self, tmp_path: pathlib.Path, patched_training, stub_trainer_cmd: list[str]
    ) -> None:
        """heartbeat() sets heartbeat_at on the run and returns True;
        returns False for an unknown run."""
        from api.training import TrainingStore

        runs_dir = patched_training["runs_dir"]
        models_dir = patched_training["models_dir"]
        record = runs_dir / "hb.json"
        record.write_text(
            json.dumps(
                {
                    "run_id": "hb",
                    "status": "completed",
                    "created_at": 1.0,
                    "started_at": 1.0,
                    "finished_at": 2.0,
                    "exit_code": 0,
                    "out_dir": str(models_dir / "m"),
                    "log_path": str(runs_dir / "hb.log"),
                    "request": {
                        "model_name": "m",
                        "input_path": "x.parquet",
                        "horizon_bars": 15,
                        "bar_seconds": 60,
                        "cv_folds": 0,
                        "purge_bars": -1,
                        "embargo_bars": 0,
                        "num_boost_round": 5,
                        "early_stopping_rounds": 5,
                    },
                }
            )
        )
        fresh = TrainingStore(
            runs_dir=runs_dir,
            models_dir=models_dir,
            trainer_cmd=stub_trainer_cmd,
        )
        run = fresh.get("hb")
        assert run is not None
        assert run.heartbeat_at is None

        ok = fresh.heartbeat("hb")
        assert ok is True
        reloaded = fresh.get("hb")
        assert reloaded is not None
        assert reloaded.heartbeat_at is not None
        assert reloaded.heartbeat_at > 0

        # Unknown run returns False.
        assert fresh.heartbeat("no-such-run") is False


# --------------------------------------------------------------------------- #
# Routes                                                                     #
# --------------------------------------------------------------------------- #


class TestTrainRoute:
    @pytest.mark.asyncio
    async def test_requires_auth(self, client: AsyncClient, patched_training) -> None:
        response = await client.post("/models/train", json={})
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_400_on_bad_input(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        patched_training,
    ) -> None:
        # Use a path inside the approved (tmp) root that doesn't exist
        # as a file -- the approved-root gate passes, but the existing
        # file-existence validator still rejects with 400 "not found".
        bad_path = patched_training["tmp_path"] / "nonexistent.parquet"
        response = await client.post(
            "/models/train",
            headers=auth_headers,
            json={
                "model_name": "m",
                "input_path": str(bad_path),
            },
        )
        assert response.status_code == 400
        assert "not found" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_202_on_success_then_completes(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        tmp_path: pathlib.Path,
        patched_training,
    ) -> None:
        store = patched_training["store"]
        input_path = _input_parquet(tmp_path)
        response = await client.post(
            "/models/train",
            headers=auth_headers,
            json={
                "model_name": "m_success",
                "input_path": str(input_path),
                "cv_folds": 2,
                "num_boost_round": 10,
                "early_stopping_rounds": 5,
            },
        )
        assert response.status_code == 202
        body = response.json()
        run_id = body["run_id"]
        assert body["status"] in ("queued", "running")
        assert body["request"]["model_name"] == "m_success"

        # Wait for completion via the store, then poll the detail route.
        await _wait_terminal(store, run_id)
        detail = await client.get(f"/models/runs/{run_id}", headers=auth_headers)
        assert detail.status_code == 200
        d = detail.json()
        assert d["status"] == "completed"
        assert isinstance(d["log_tail"], list)
        assert any("stub trainer" in line for line in d["log_tail"])

    @pytest.mark.asyncio
    async def test_429_when_at_capacity(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        tmp_path: pathlib.Path,
        patched_training,
    ) -> None:
        store = patched_training["store"]
        input_path = _input_parquet(tmp_path)
        # First request occupies the single slot.
        first = await client.post(
            "/models/train",
            headers=auth_headers,
            json={
                "model_name": "first",
                "input_path": str(input_path),
                "cv_folds": 0,
                "num_boost_round": 5,
                "early_stopping_rounds": 5,
            },
        )
        assert first.status_code == 202

        # Second comes right behind -> 429.
        second = await client.post(
            "/models/train",
            headers=auth_headers,
            json={
                "model_name": "second",
                "input_path": str(input_path),
                "cv_folds": 0,
                "num_boost_round": 5,
                "early_stopping_rounds": 5,
            },
        )
        assert second.status_code == 429
        # And the message is operator-friendly.
        assert "in flight" in second.json()["detail"]

        # Drain the first run before the test ends.
        await _wait_terminal(store, first.json()["run_id"])


class TestRunsRoutes:
    @pytest.mark.asyncio
    async def test_runs_requires_auth(
        self, client: AsyncClient, patched_training
    ) -> None:
        response = await client.get("/models/runs")
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_runs_listing_empty(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        patched_training,
    ) -> None:
        response = await client.get("/models/runs", headers=auth_headers)
        assert response.status_code == 200
        body = response.json()
        assert body["runs"] == []
        assert body["summary"]["count"] == 0

    @pytest.mark.asyncio
    async def test_runs_listing_after_completion(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        tmp_path: pathlib.Path,
        patched_training,
    ) -> None:
        store = patched_training["store"]
        input_path = _input_parquet(tmp_path)
        post = await client.post(
            "/models/train",
            headers=auth_headers,
            json={
                "model_name": "m",
                "input_path": str(input_path),
                "cv_folds": 0,
                "num_boost_round": 5,
                "early_stopping_rounds": 5,
            },
        )
        run_id = post.json()["run_id"]
        await _wait_terminal(store, run_id)

        listing = await client.get("/models/runs", headers=auth_headers)
        assert listing.status_code == 200
        body = listing.json()
        assert body["summary"]["completed"] == 1
        assert body["runs"][0]["run_id"] == run_id

        # status filter still works.
        filtered = await client.get("/models/runs?status=failed", headers=auth_headers)
        assert filtered.json()["runs"] == []

    @pytest.mark.asyncio
    async def test_run_detail_404(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        patched_training,
    ) -> None:
        response = await client.get("/models/runs/no_such_id", headers=auth_headers)
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_runs_listing_rejects_bad_status(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        patched_training,
    ) -> None:
        response = await client.get("/models/runs?status=banana", headers=auth_headers)
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_runs_listing_rejects_bad_limit(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        patched_training,
    ) -> None:
        response = await client.get("/models/runs?limit=0", headers=auth_headers)
        assert response.status_code == 400
        response = await client.get("/models/runs?limit=999", headers=auth_headers)
        assert response.status_code == 400
