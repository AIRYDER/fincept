# Receipt Directory — c508103f (Swarm Scaffold)

This directory is the structured home for live RunPod probe receipts produced while validating the training worker image built from commit `c508103fbac4b38b8f3c369f216f6e18177f72a4`.

It mirrors the receipt directory layout defined in `docs/runpod-fix-plan/01-validation-baseline.md` section 12, adapted for the swarm validation run. Per that section, receipts are stored outside source code changes unless explicitly asked to commit them — these files are evidence only and are not part of any source change.

## Identity

- Branch: `fix/test-harness-optional-deps-guards`
- Commit SHA: `c508103fbac4b38b8f3c369f216f6e18177f72a4`
- Image tag: `ghcr.io/airyder/fincept/quant-foundry-training:c508103fbac4b38b8f3c369f216f6e18177f72a4`

## Directory Layout

The Orchestrator's live probe will drop JSONL/JSON receipts into this directory. The expected receipt files are:

| File | Purpose | Source step (baseline section) |
| --- | --- | --- |
| `repo-state.txt` | `git status --short --branch` output and exact SHA | §1 Record Repo State |
| `local-tests.txt` | Local handler test transcripts and exit codes (diagnostic return-only, diagnostic full, production canary, layered 0–5) | §3 Run Local Handler Tests |
| `ruff.txt` | `uv run ruff check runpod/quant-foundry-training scripts` output | §4 Run Static Checks |
| `workflow.txt` | GitHub Actions `build-runpod-training.yml` run id, SHA, image tag, success/failure | §5 Check Image Build Workflow Status |
| `endpoint-create-redacted.json` | Fresh debug endpoint id, name, and redacted template input (registry auth id redacted) | §6 Create A Fresh Debug Endpoint |
| `health-before.json` | First healthy `/health` response after endpoint creation, before Layer 0 dispatch | §6 / §7 Probe Endpoint Health |
| `layer0-probe.jsonl` | Layer 0 probe transcript: `probe_start`, `health_before`, `run_response`, every `status`, every `health`, `probe_end`/`probe_timeout`/`probe_error` | §8 Run Layer Probe |
| `cleanup.json` | Debug endpoint scale-down (`workersMin=0`, `workersMax=0`) confirmation and post-cleanup health | §11 Scale Debug Endpoints Down |
| `interpretation.md` | Short interpretation naming the proven root cause or the still-open failing boundary | §12 / acceptance criteria |

Additional layer receipts (layers 1–5) and the full canary receipt, if Layer 0 passes, follow the same JSONL shape as `layer0-probe.jsonl` and are named `layer1-probe.jsonl` through `layer5-probe.jsonl` and `canary-probe.jsonl` respectively.

## Notes

- No API keys, callback secrets, or unredacted registry auth ids belong in any file in this directory. Registry auth ids must be redacted in shared summaries.
- Callback signatures may appear only in local-only evidence where explicitly required; never in shared summaries.
- The `interpretation.md` file in this directory is the structured acceptance template. The Orchestrator fills the "Live Probe Results" placeholders after the probe completes.
