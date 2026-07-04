# A7 — Minimal train_model Job: PASSED

Date: 2026-07-04
Task: A7 (v6 task queue) — the single remaining critical live unknown after
the 6/6 canaries and the A6 gpu_healthcheck.
Tool: `runpod/quant-foundry-training/run_train_model.py`
Image: `ghcr.io/airyder/fincept/quant-foundry-training:6dbec436c92b57a788b84622338baacc3df8665d`
(exact 40-char SHA, same image as the 6/6 canaries and the A6 gpu_healthcheck)

## Result

**TRAIN_MODEL PASSED.** The full training pipeline — dataset loading →
trainer.fit (RealLightGBMTrainer walk-forward + final fit) → model export
(artifact write, sha256 re-verification, signed write receipt) — works live
inside the production container.

| Field | Value |
|-------|-------|
| Endpoint | `sj5lj1vxhydaja` (fresh, GPU ADA_24, workersMin=1/max=1, QUEUE_DELAY:4, idle 300s, 20 GB disk) |
| Job | `1363ef31-c7aa-4e57-acd1-c090a825c6e2-u1` |
| Status | `COMPLETED` |
| executionTime | 1656 ms |
| delayTime | 13893 ms |
| Worker | `puo9wdtddc2ag9`, `unhealthy=0` throughout (before, during, after) |
| Job id (payload) | `qf:a7-train:6dbec436:001` |

## What the job exercised (pipeline proof)

The payload was an **implicit train_model request** (no `task` field — the
`RunPodTrainingRequest` schema forbids extra fields; a missing task is the
implicit training dispatch). Handler-level extensions:
`inline_dataset_csv` (300-row deterministic synthetic dataset, seed 42,
header `timestamp,f1,f2,f3,label`), `n_folds=2`,
`output_prefix=/tmp/a7-train-artifacts`. Mode:
`extra_constraints.training_mode=canary`. The image env sets
`QUANT_FOUNDRY_USE_REAL_TRAINER=true`, so the REAL trainer ran (not the
LocalTrainer canary stub — confirmed by `trainer: real_lightgbm` in the
dossier metadata and the 337 KB pickled lightgbm booster).

1. **Dataset loading** — inline CSV → temp file →
   `RealLightGBMTrainer._load_csv` (300 rows, 3 features, binary label).
   Dossier metadata: `n_features=3`, `n_rows=300`.
2. **trainer.fit** — walk-forward validation with 2 heuristic folds
   (`fold_source=heuristic`, `fold_best_iterations=[100, 100]`) + final
   model fit. Metrics: accuracy 0.835, brier 0.122, logloss 0.386.
3. **Model export** — pickle → `VolumeArtifactWriter` →
   `file:///tmp/a7-train-artifacts/model.pkl`, sha256
   `ac0b69ba8b52f20e...9921274f`, 337368 bytes, byte-for-byte sha
   re-verification passed, HMAC write receipt present
   (`0196331085d6...c94c3d2c`).
4. **Signed contracts intact** — `callback_signature` present, typed
   callback (`schema_version 1.0`) present with `promotion_eligible=false`
   (canary mode, correct), SecurityPreflight `passed=true`, no forbidden
   env vars, redacted config summary only (no secret values anywhere in
   this bundle).

## Determinism cross-check (extra confidence)

A local in-process smoke of the SAME payload (run before the live dispatch,
`--local` flag) produced the **identical model sha256**
(`ac0b69ba8b52f20e898ccde31fabc92f574129a3e9a741f7b769e2519921274f`) and
identical metrics. The live worker and the local environment train
bit-identical models from the same seed/dataset — the pipeline is
deterministic across environments.

## Timeline

- Attempt #1 (endpoint `8wsrepx5dc6r2a`): worker stuck `initializing=1` for
  155s+ and missed the canary tool's 180s ready window (cold pull of the
  ~6 GB torch-cu124 image on a fresh host). No job was dispatched; endpoint
  scaled down + deleted. NOT a handler/training failure. The tool's ready
  timeout was raised to 600s (`TRAIN_READY_TIMEOUT_S`).
- Attempt #2 (endpoint `sj5lj1vxhydaja`): ready in 65s, job dispatched,
  IN_QUEUE for ~13.9s (worker cold-starting the handler process), then
  COMPLETED with executionTime 1656 ms. Worker `unhealthy=0` in every
  probe sample (see `probe.jsonl`).

## Cleanup

Endpoint scaled to 0/0 immediately. The in-run `deleteEndpoint` failed
transiently ("Failed to terminate resources. Try again." — worker still
spinning down); a follow-up cleanup pass deleted the endpoint and both A7
templates. Verified: no `qf-a7train-*` endpoints or templates remain. See
`cleanup.json`.

## Acceptance checklist

- [x] Job reached COMPLETED (not IN_QUEUE, not FAILED)
- [x] Worker `unhealthy=0` throughout
- [x] Dataset loading exercised (real CSV load, 300x3 + label)
- [x] trainer.fit exercised (real lightgbm walk-forward + final fit)
- [x] Model export exercised (artifact write + sha verify + write receipt)
- [x] No secrets in any receipt (redaction discipline maintained)
- [x] Endpoint scaled down and deleted (follow-up pass)
- [x] Receipt bundle written and redacted

## Files

- `endpoint-create-redacted.json` / `template-redacted.txt` — endpoint/template config (redacted)
- `health-before.json` — `ready=1, idle=1, unhealthy=0` pre-dispatch
- `run-response.json` — dispatch receipt (dataset shape recorded, not the full CSV body)
- `probe.jsonl` — raw poll events (IN_QUEUE x3 → COMPLETED, unhealthy=0 throughout)
- `status-final.json` — full job output: artifact_result, typed_callback, preflight_result
- `train-model-result.json` — extracted pipeline evidence (artifact uri/sha/size, metrics)
- `health-after.json` — post-job worker health (`unhealthy=0`)
- `cleanup.json` — scale-down + deletion record (including follow-up pass)

## What this closes

A7 was the last critical live unknown. The chain is now fully proven live
on the `6dbec436` production image: container boot → SDK job loop →
production handler as direct entrypoint (6/6 canaries) → GPU access (A6)
→ **full training pipeline (A7)**. The `parents[5]` fix is validated for
the actual product path, not just the canary path.
