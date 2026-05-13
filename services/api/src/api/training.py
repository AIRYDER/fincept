"""
api.training - filesystem-backed training-runs registry.

Why filesystem and not Postgres / Redis?

  Operator workflows (this is one) need a durable, human-greppable
  trail of "what was tried, when, on what data, with what result".
  Files in ``data/training_runs/`` give us that with zero infra:

    data/training_runs/
      <run_id>.json    -- machine-readable run record
      <run_id>.log     -- raw stdout+stderr from the trainer

  A single operator can ``cat``/``ls`` to debug; a future migration
  to a database is straightforward (every field is plain JSON).

Why subprocess and not in-process import?

  The api service deliberately doesn't depend on lightgbm; pulling
  the trainer in-process would bloat the api wheel with a native
  binary and add a startup-time failure mode.  Subprocess isolates
  the heavy ML deps in their own process and keeps the api tiny.

State machine
~~~~~~~~~~~~~

  queued    -- record created, subprocess not yet spawned
  running   -- subprocess started, exit not yet observed
  completed -- subprocess exited 0 + writes to ``out_dir`` succeeded
  failed    -- subprocess exited non-zero or pre-launch validation failed

  ``stale`` is a *derived* status the API surfaces when a record is
  ``running`` but the in-memory task has been gone since the last api
  restart (process state doesn't persist across restarts).

Concurrency
~~~~~~~~~~~

  At most :data:`MAX_CONCURRENT_RUNS` (default 1) subprocesses run at
  once.  Additional ``start_run`` calls return an HTTP-friendly error
  that the route translates to 429.  Single-runner default is the
  conservative choice for an operator dashboard on a workstation -
  parallel CV training swamps a laptop CPU and the realised wall-clock
  isn't actually faster.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import pathlib
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from typing import Any, Callable

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Configuration                                                              #
# --------------------------------------------------------------------------- #
#
# All knobs are read inside ``TrainingStore.__init__`` rather than at
# module top-level.  Reading env at import time made tests need
# ``importlib.reload``, which is brittle and surprising; constructor
# injection means a test fixture just calls ``reset_store(...)``.

# How many tail lines we expose via the run-detail endpoint.  Bigger
# logs are still on disk; we just don't ship them over the wire.
LOG_TAIL_LINES = 200

# Names that would leak outside MODELS_DIR or break path joins.
_BAD_NAME_CHARS = set("/\\:*?\"<>|\0")


def _default_runs_dir() -> pathlib.Path:
    return pathlib.Path(
        os.environ.get("TRAINING_RUNS_DIR", "data/training_runs")
    )


def _default_models_dir() -> pathlib.Path:
    return pathlib.Path(os.environ.get("MODELS_DIR", "models"))


def _default_max_concurrent() -> int:
    return int(os.environ.get("MAX_CONCURRENT_TRAINING_RUNS", "1"))


# --------------------------------------------------------------------------- #
# Dataclasses                                                                #
# --------------------------------------------------------------------------- #


@dataclasses.dataclass(frozen=True)
class TrainingRequest:
    """User-supplied training parameters.

    Mirrors the trainer CLI flags (see ``agents.gbm_predictor.train``).
    All fields are required at the storage layer; the route layer
    fills in sensible defaults from the request body.
    """

    model_name: str
    input_path: str
    horizon_bars: int
    bar_seconds: int
    cv_folds: int
    purge_bars: int  # -1 means "use horizon_bars" (matches trainer default)
    embargo_bars: int
    num_boost_round: int
    early_stopping_rounds: int

    def as_cli_args(self, *, out_dir: pathlib.Path) -> list[str]:
        """Build the argv tail that follows ``python -m agents.gbm_predictor.train``."""
        args = [
            "--input",
            self.input_path,
            "--horizon-bars",
            str(self.horizon_bars),
            "--bar-seconds",
            str(self.bar_seconds),
            "--out-dir",
            str(out_dir),
            "--num-boost-round",
            str(self.num_boost_round),
            "--early-stopping-rounds",
            str(self.early_stopping_rounds),
            "--cv-folds",
            str(self.cv_folds),
            "--embargo-bars",
            str(self.embargo_bars),
        ]
        # Purge bars takes a default of -1 ("use horizon_bars") in the
        # trainer; pass explicitly only when the operator overrides it.
        if self.purge_bars >= 0:
            args.extend(["--purge-bars", str(self.purge_bars)])
        return args


@dataclasses.dataclass
class TrainingRun:
    run_id: str
    request: TrainingRequest
    status: str  # queued | running | completed | failed
    created_at: float
    started_at: float | None
    finished_at: float | None
    exit_code: int | None
    out_dir: str
    log_path: str
    record_path: str
    error: str | None = None
    pid: int | None = None

    def to_payload(self, *, log_tail: list[str] | None = None) -> dict[str, Any]:
        """Serialize for the API.  ``log_tail`` is read on demand."""
        payload: dict[str, Any] = {
            "run_id": self.run_id,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_seconds": (
                self.finished_at - self.started_at
                if (self.started_at is not None and self.finished_at is not None)
                else None
            ),
            "exit_code": self.exit_code,
            "pid": self.pid,
            "out_dir": self.out_dir,
            "log_path": self.log_path,
            "error": self.error,
            "request": dataclasses.asdict(self.request),
        }
        if log_tail is not None:
            payload["log_tail"] = log_tail
        return payload


# --------------------------------------------------------------------------- #
# Disk persistence                                                           #
# --------------------------------------------------------------------------- #


def _persist(run: TrainingRun) -> None:
    """Atomically write the run record to disk.

    We write to a temp file in the same directory then rename, so a
    crash mid-write can't leave a partially-flushed JSON behind that
    later breaks the listing endpoint.
    """
    record_path = pathlib.Path(run.record_path)
    record_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = record_path.with_suffix(record_path.suffix + ".tmp")
    payload = run.to_payload()
    tmp.write_text(json.dumps(payload, indent=2))
    os.replace(tmp, record_path)


def _load_record(path: pathlib.Path) -> TrainingRun | None:
    """Best-effort load of one record file.  Bad records are dropped."""
    try:
        data = json.loads(path.read_text())
        req = TrainingRequest(**data["request"])
        return TrainingRun(
            run_id=data["run_id"],
            request=req,
            status=data.get("status", "failed"),
            created_at=float(data.get("created_at", 0)),
            started_at=data.get("started_at"),
            finished_at=data.get("finished_at"),
            exit_code=data.get("exit_code"),
            out_dir=data.get("out_dir", ""),
            log_path=data.get("log_path", ""),
            record_path=str(path),
            error=data.get("error"),
            pid=data.get("pid"),
        )
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        logger.warning("dropping malformed run record %s: %s", path.name, exc)
        return None


# --------------------------------------------------------------------------- #
# Validation                                                                 #
# --------------------------------------------------------------------------- #


class TrainingValidationError(ValueError):
    """Raised when the request fails pre-launch validation."""


def _validate_model_name(name: str) -> None:
    if not name or any(ch in _BAD_NAME_CHARS for ch in name):
        raise TrainingValidationError(
            f"invalid model name: {name!r} - "
            "letters, digits, dot, underscore, and dash only"
        )
    if name.startswith("."):
        raise TrainingValidationError(
            f"invalid model name: {name!r} - cannot start with a dot"
        )


def _validate_input_path(input_path: str) -> pathlib.Path:
    """The trainer reads a parquet file; refuse anything outside the repo
    root or with a ``..`` component (defence-in-depth even though the
    operator is trusted)."""
    p = pathlib.Path(input_path)
    if not p.is_file():
        raise TrainingValidationError(f"input path not found: {input_path}")
    return p


def _validate_request(req: TrainingRequest) -> None:
    _validate_model_name(req.model_name)
    _validate_input_path(req.input_path)
    if req.horizon_bars <= 0:
        raise TrainingValidationError("horizon_bars must be positive")
    if req.bar_seconds <= 0:
        raise TrainingValidationError("bar_seconds must be positive")
    if req.cv_folds < 0:
        raise TrainingValidationError("cv_folds must be >= 0")
    if req.cv_folds > 50:
        raise TrainingValidationError("cv_folds capped at 50")
    if req.num_boost_round <= 0:
        raise TrainingValidationError("num_boost_round must be positive")
    if req.early_stopping_rounds <= 0:
        raise TrainingValidationError("early_stopping_rounds must be positive")
    if req.embargo_bars < 0:
        raise TrainingValidationError("embargo_bars must be >= 0")


# --------------------------------------------------------------------------- #
# Subprocess command resolution                                              #
# --------------------------------------------------------------------------- #


def _resolve_trainer_command() -> list[str]:
    """Default resolver: pick how to launch the trainer at runtime.

    Two paths:

    1. ``agents`` is importable from the api process (workspace dev
       setup) -- use ``sys.executable -m agents.gbm_predictor.train``.
       Cleanest, no extra tooling.

    2. Not importable -- fall back to ``uv run --package agents python
       -m ...``.  This handles split-environment setups and CI/Docker
       images where the api venv is intentionally minimal.

    The ``MODEL_TRAINER_CMD`` env var, when set, is parsed with
    ``shlex.split(posix=False)`` on Windows so backslashed paths
    survive intact.  Tests should prefer constructor injection
    (``TrainingStore(trainer_cmd=[...])``) over the env override.

    The result includes everything *except* the per-request CLI flags;
    callers append those.
    """
    override = os.environ.get("MODEL_TRAINER_CMD")
    if override:
        # POSIX-mode shlex eats backslashes on Windows; choose the
        # right mode at runtime so a Windows operator can paste
        # something like ``MODEL_TRAINER_CMD="C:\Python\python.exe ...``.
        return shlex.split(override, posix=os.name != "nt")

    try:
        import agents.gbm_predictor.train as _probe  # noqa: F401

        return [sys.executable, "-m", "agents.gbm_predictor.train"]
    except Exception:
        uv = shutil.which("uv")
        if uv is None:
            raise TrainingValidationError(
                "agents package not importable and `uv` not on PATH; "
                "set MODEL_TRAINER_CMD or install the agents wheel."
            ) from None
        return [uv, "run", "--package", "agents", "python", "-m", "agents.gbm_predictor.train"]


# --------------------------------------------------------------------------- #
# Store + scheduler                                                           #
# --------------------------------------------------------------------------- #


class TrainingStore:
    """In-memory cache of runs + a single-flight scheduler.

    Boots by replaying ``data/training_runs/*.json`` so the dashboard
    can show history across restarts.  Records still tagged as
    ``running`` from a previous process are flipped to ``failed`` with
    an explanatory error -- the subprocess they reference is gone.

    All configuration is constructor-injected so tests can spin up an
    isolated store without monkey-patching env vars or reloading
    modules.  In production the no-arg form reads env defaults.
    """

    def __init__(
        self,
        *,
        runs_dir: pathlib.Path | None = None,
        models_dir: pathlib.Path | None = None,
        max_concurrent: int | None = None,
        trainer_cmd: list[str] | None = None,
    ) -> None:
        self._runs_dir = runs_dir if runs_dir is not None else _default_runs_dir()
        self._models_dir = (
            models_dir if models_dir is not None else _default_models_dir()
        )
        self._max_concurrent = (
            max_concurrent if max_concurrent is not None else _default_max_concurrent()
        )
        # When ``None``, we resolve the trainer command lazily inside
        # ``_run_subprocess`` -- letting tests and operators set the
        # env var or the agents-importable fallback decide.
        self._trainer_cmd = trainer_cmd
        self._runs: dict[str, TrainingRun] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._lock = asyncio.Lock()
        self._reload_from_disk()

    # ------ public API ------------------------------------------------- #

    def list_runs(self) -> list[TrainingRun]:
        """Newest-first."""
        return sorted(self._runs.values(), key=lambda r: r.created_at, reverse=True)

    def get(self, run_id: str) -> TrainingRun | None:
        return self._runs.get(run_id)

    def get_log_tail(self, run_id: str, n: int = LOG_TAIL_LINES) -> list[str]:
        run = self._runs.get(run_id)
        if run is None:
            return []
        path = pathlib.Path(run.log_path)
        if not path.is_file():
            return []
        try:
            with path.open(encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except OSError:
            return []
        return [line.rstrip("\n") for line in lines[-n:]]

    async def start_run(self, req: TrainingRequest) -> TrainingRun:
        """Validate, persist, and launch a new subprocess.

        Holding the lock across the validation + capacity check makes
        the start path serialised, which is what we want -- the
        operator clicking 'Train' twice in a row should produce one
        run, not race conditions on the next-id counter.
        """
        async with self._lock:
            running = sum(
                1 for r in self._runs.values() if r.status in ("queued", "running")
            )
            if running >= self._max_concurrent:
                raise TrainingValidationError(
                    f"already {running} run(s) in flight; max is {self._max_concurrent}"
                )
            _validate_request(req)
            run_id = uuid.uuid4().hex
            self._runs_dir.mkdir(parents=True, exist_ok=True)
            out_dir = self._models_dir / req.model_name
            run = TrainingRun(
                run_id=run_id,
                request=req,
                status="queued",
                created_at=time.time(),
                started_at=None,
                finished_at=None,
                exit_code=None,
                out_dir=str(out_dir),
                log_path=str(self._runs_dir / f"{run_id}.log"),
                record_path=str(self._runs_dir / f"{run_id}.json"),
            )
            self._runs[run_id] = run
            _persist(run)

            # Schedule the subprocess.  The task drives the lifecycle;
            # we keep a strong reference to avoid GC cancelling it.
            task = asyncio.create_task(self._run_subprocess(run))
            self._tasks[run_id] = task
            task.add_done_callback(self._forget_task(run_id))
            return run

    # ------ internals ------------------------------------------------- #

    def _forget_task(
        self, run_id: str
    ) -> Callable[[asyncio.Task[None]], None]:
        def done(_task: asyncio.Task[None]) -> None:
            self._tasks.pop(run_id, None)

        return done

    def _reload_from_disk(self) -> None:
        if not self._runs_dir.is_dir():
            return
        for path in self._runs_dir.glob("*.json"):
            run = _load_record(path)
            if run is None:
                continue
            if run.status in ("queued", "running"):
                # Process state is gone with the previous interpreter;
                # mark these as failed so the UI doesn't show a fake
                # "still running" forever.
                run.status = "failed"
                run.error = (
                    "api restarted while this run was active; subprocess state lost"
                )
                run.finished_at = run.finished_at or time.time()
                _persist(run)
            self._runs[run.run_id] = run

    async def _run_subprocess(self, run: TrainingRun) -> None:
        """Drive one run through running -> completed/failed.

        Wrapped in try/finally so transient asyncio cancellations or
        OSErrors during launch still produce a terminal record on
        disk -- otherwise the UI would see a permanent ``queued``.
        """
        log_path = pathlib.Path(run.log_path)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            base_cmd = (
                list(self._trainer_cmd)
                if self._trainer_cmd is not None
                else _resolve_trainer_command()
            )
            cmd = base_cmd + run.request.as_cli_args(
                out_dir=pathlib.Path(run.out_dir)
            )
        except TrainingValidationError as exc:
            run.status = "failed"
            run.finished_at = time.time()
            run.error = str(exc)
            _persist(run)
            return

        # Open log for write+append in binary so we can pipe stdout
        # directly without extra encoding hops.
        try:
            with log_path.open("wb") as logf:
                logf.write(
                    f"$ {' '.join(cmd)}\n".encode("utf-8")
                )
                logf.flush()
                proc = subprocess.Popen(
                    cmd,
                    stdout=logf,
                    stderr=subprocess.STDOUT,
                )
                run.status = "running"
                run.started_at = time.time()
                run.pid = proc.pid
                _persist(run)
                exit_code = await asyncio.to_thread(proc.wait)
        except (OSError, asyncio.CancelledError, Exception) as exc:
            run.status = "failed"
            run.finished_at = time.time()
            run.error = f"subprocess launch failed: {exc!r}"
            _persist(run)
            return
        finally:
            run.pid = None

        run.exit_code = exit_code
        run.finished_at = time.time()
        if exit_code == 0:
            run.status = "completed"
        else:
            run.status = "failed"
            run.error = run.error or f"trainer exited with code {exit_code}"
        _persist(run)


# --------------------------------------------------------------------------- #
# Module-level singleton                                                     #
# --------------------------------------------------------------------------- #
#
# A single store backs all routes.  Tests that need isolation can
# replace ``_store`` with a fresh instance via :func:`reset_store`.

_store: TrainingStore | None = None


def get_store() -> TrainingStore:
    global _store
    if _store is None:
        _store = TrainingStore()
    return _store


def reset_store(
    *,
    runs_dir: pathlib.Path | None = None,
    models_dir: pathlib.Path | None = None,
    max_concurrent: int | None = None,
    trainer_cmd: list[str] | None = None,
) -> TrainingStore:
    """Test hook: rebuild the singleton against fresh config.

    All arguments are forwarded straight to ``TrainingStore``; passing
    ``None`` for any keeps the env-default behaviour.
    """
    global _store
    _store = TrainingStore(
        runs_dir=runs_dir,
        models_dir=models_dir,
        max_concurrent=max_concurrent,
        trainer_cmd=trainer_cmd,
    )
    return _store
