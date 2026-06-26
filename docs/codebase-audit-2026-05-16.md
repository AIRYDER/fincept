# Fincept Terminal Code Audit - 2026-05-16

Scope: code only, not docs. I reviewed the local startup flow, OpenBB tooling, and the main API file-boundary handlers to look for contract drift, security boundary gaps, and operational mismatches.

This was not a full runtime certification. A targeted pytest slice could not start because `fakeredis` is missing from the current Python environment, so the API test suite was blocked before it reached the relevant backtest and training cases.

## What I Reviewed

- `scripts/start.ps1`
- `scripts/start_feature.ps1`
- `scripts/status.ps1`
- `scripts/openbb_live_proof.py`
- `libs/fincept-tools/src/fincept_tools/research/openbb.py`
- `services/api/src/api/routes/backtest.py`
- `services/api/src/api/training.py`

## Executive Summary

The codebase is broadly in good shape, but three issues stood out:

1. The OpenBB default port has split into two different assumptions across the repo.
2. The backtest endpoint accepts an arbitrary local parquet path with no filesystem boundary check.
3. The training path validator claims to confine inputs but only checks that the file exists.

The first issue is mostly operational, but the latter two are real boundary problems. They widen the local blast radius and make it easier for a caller to point the API at files that were never meant to be part of the training or backtest flow.

## Findings

### 1. OpenBB default port is split across the launcher and the tool layer

Severity: Medium

Locations:

- `scripts/start.ps1:184-189`
- `scripts/start.ps1:218-229`
- `scripts/start.ps1:407-469`
- `scripts/start_feature.ps1:81-126`
- `libs/fincept-tools/src/fincept_tools/research/openbb.py:40-188`
- `scripts/status.ps1:6-14`
- `scripts/openbb_live_proof.py:4-7`

Issue:

`scripts/start.ps1` now defaults OpenBB to `http://127.0.0.1:6901`, but the feature launcher, status checker, live proof script, and the OpenBB tool default still assume `6900`. That means the stack can disagree about where OpenBB lives, which creates false negatives in the health/status path and makes the operator experience depend on which helper was used last.

Why it matters:

- `start.ps1` can publish `OPENBB_API_URL=6901` into the process environment.
- `start_feature.ps1` still spawns OpenBB on `6900`.
- `fincept_tools.research.openbb` still falls back to `6900` if no env var is present.
- `status.ps1` still probes `6900`, so it can report a healthy process while the launcher is actually using a different port.

Recommended fix:

- Centralize the OpenBB base URL or default port in one shared source of truth.
- Consume that source from `start.ps1`, `start_feature.ps1`, `status.ps1`, `openbb_live_proof.py`, and `fincept_tools.research.openbb`.
- Add a regression test that asserts the runtime default is identical across the launcher and the tool layer.

Impact:

- Operational reliability
- Dashboard/status correctness
- Local developer experience

### 2. Backtest input path is unconstrained

Severity: Medium

Location: `services/api/src/api/routes/backtest.py:112-135`

Issue:

`POST /backtest/run` accepts `bars_path` and turns it directly into `pathlib.Path(body.bars_path)` with only an existence check. The route comment says it is supposed to work on repo-relative parquet files, but the implementation does not enforce that boundary.

Why it matters:

- A caller can point the API at any readable local file.
- That widens the blast radius of an authenticated request.
- It makes the endpoint harder to reason about because the documented contract is narrower than the actual code path.

Recommended fix:

- Resolve the path against an approved root, such as a `data/` directory or repo-root data allowlist.
- Reject traversal and out-of-root paths with a 400 before the runner sees them.
- Add tests for `../` traversal and absolute-path rejection.

Impact:

- Local filesystem boundary
- Authenticated API abuse surface

### 3. Training input validation does not enforce its own boundary

Severity: Medium

Location: `services/api/src/api/training.py:252-259`

Issue:

The `_validate_input_path()` docstring says the trainer should refuse anything outside the repo root or with `..`, but the implementation only checks `Path(input_path).is_file()`. That means any existing file on disk is accepted as long as the API process can read it.

Why it matters:

- The route is already authenticated, but the path check is still a real trust boundary.
- The current behavior does not match the stated contract.
- A training request can be used to feed the subprocess arbitrary local files, which increases the chance of accidental disclosure through logs, stack traces, or downstream parsing errors.

Recommended fix:

- Enforce a resolved path prefix check against an approved root.
- Make the code and docstring say the same thing.
- Add tests for traversal, absolute paths outside the root, and valid repo-local fixtures.

Impact:

- Local filesystem boundary
- Data exposure risk
- Contract drift

## Improvements That Can Be Made

1. Introduce a shared OpenBB config helper and remove the hardcoded 6900/6901 split.
2. Add a reusable safe-path helper for repo-bound file inputs, then use it in both training and backtest routes.
3. Tighten `scripts/status.ps1` and `scripts/openbb_live_proof.py` so they cannot silently drift from the launcher defaults again.
4. Add regression tests for the OpenBB port contract and for invalid training/backtest paths.
5. Re-run the API test slice once `fakeredis` is available in the environment.

## What I Could Not Fully Verify

- The API test slice could not start because `services/api/tests/conftest.py` imports `fakeredis`, which is not installed in the current environment.
- I did not run a live OpenBB stack, so the port mismatch is a code-level audit finding rather than a live-runtime proof.
- I did not exercise the browser/dashboard surfaces; this review stayed focused on code paths and boundary checks.

## Bottom Line

The repo is close, but not quite contract-tight yet. The OpenBB mismatch is annoying and operationally visible; the file-path handling in backtest and training is the more important issue because it widens the local trust boundary. Those are both small, surgical fixes, and they are worth doing before more features accumulate on top of the current defaults.
