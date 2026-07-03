# RunPod Fix Plan: Swarm Task Cards

Last updated: 2026-07-03

These cards are written for smaller agents. Each agent should do only its card, leave receipts, and stop when the acceptance criteria are met or the stop condition triggers.

## Card 1: Baseline Auditor

Agent name / role:

- Baseline Auditor

Objective:

- Establish the current repo, local handler, Dockerfile, workflow, and known-failed baseline before any edits.

Context needed:

- Known failed SHA: `412080c61a38cd138a92d43df8242503d27f71d2`
- Known failed endpoint: `zbpy7m8s8dps7k`
- Known failed layer: `diag_layer=0`
- Current plan docs: `docs/runpod-fix-plan/00-system-context.md` and `docs/runpod-fix-plan/01-validation-baseline.md`

Files they may edit:

- None by default.
- Optional receipts under `reports/runpod-test-runs/` if the operator wants local evidence files.

Files they must not edit:

- `runpod/quant-foundry-training/Dockerfile`
- `runpod/quant-foundry-training/handler.py`
- `runpod/quant-foundry-training/handler_layered.py`
- `.github/workflows/build-runpod-training.yml`
- inference worker files

Commands to run:

```powershell
git status --short --branch
git rev-parse HEAD
git show 412080c61a38cd138a92d43df8242503d27f71d2:runpod/quant-foundry-training/Dockerfile |
  Select-String -Pattern "FROM|handler_layered|handler_full|runpod==|HEALTHCHECK|ENTRYPOINT" -Context 2
Select-String -Path runpod/quant-foundry-training/Dockerfile `
  -Pattern "FROM|handler_layered|handler_full|runpod==|HEALTHCHECK|ENTRYPOINT" `
  -Context 2
uv run ruff check runpod/quant-foundry-training scripts
```

Evidence to collect:

- branch and SHA
- dirty/untracked files list
- failed baseline Dockerfile shape
- current Dockerfile shape
- ruff result

Acceptance criteria:

- The team knows whether the current branch is still the failed layered shape or has already moved past it.
- No files outside optional receipts were changed.

Rollback plan:

- No rollback required if no files were edited.

Common mistakes to avoid:

- Do not assume the current Dockerfile still has a healthcheck.
- Do not clean up unrelated untracked scratch files.
- Do not infer live behavior from local tests.

## Card 2: Healthcheck Fix Agent

Agent name / role:

- Healthcheck Fix Agent

Objective:

- Create the first single-variable code change: remove only the Docker healthcheck from the known failed layered training image shape.

Context needed:

- This card applies only if the target branch/SHA still has the layered Dockerfile shape with the import-based Docker healthcheck.
- If the Dockerfile already has no Docker healthcheck, stop and hand off to Endpoint Creator / Runner.

Files they may edit:

- `runpod/quant-foundry-training/Dockerfile`

Files they must not edit:

- `runpod/quant-foundry-training/handler.py`
- `runpod/quant-foundry-training/handler_layered.py`
- `runpod/quant-foundry-training/handler_diagnostic.py`
- `scripts/*`
- `.github/workflows/*`
- `runpod/quant-foundry-inference/*`
- UI/app/product files

Commands to run:

```powershell
git status --short --branch
git rev-parse HEAD
Select-String -Path runpod/quant-foundry-training/Dockerfile `
  -Pattern "HEALTHCHECK|handler_layered|handler_full|ENTRYPOINT" `
  -Context 3
uv run ruff check runpod/quant-foundry-training scripts
```

Evidence to collect:

- before/after Dockerfile excerpt
- `git diff -- runpod/quant-foundry-training/Dockerfile`
- ruff result

Acceptance criteria:

- Dockerfile no longer defines an import-based Docker `HEALTHCHECK`.
- Handler mapping, base image, SDK version, and entrypoint are unchanged.
- Diff is Dockerfile-only.

Rollback plan:

```powershell
git restore --source=HEAD~1 -- runpod/quant-foundry-training/Dockerfile
```

Use rollback only if this agent created the commit and the team explicitly wants to revert it. Do not revert user or other-agent changes.

Common mistakes to avoid:

- Do not change base image.
- Do not change `runpod==1.7.13`.
- Do not switch entrypoints.
- Do not edit handler logic.
- Do not claim the fix works before a live Layer 0 probe completes.

## Card 3: Endpoint Creator / Runner

Agent name / role:

- Endpoint Creator / Runner

Objective:

- Create a fresh RunPod endpoint from the exact SHA-tagged image using the known working template shape.

Context needed:

- Image must be built and pushed by `.github/workflows/build-runpod-training.yml`.
- Use bearer auth only.
- Copy registry auth from an existing working endpoint; redact the id in summaries.

Files they may edit:

- None.
- Optional receipts under `reports/runpod-test-runs/`.

Files they must not edit:

- Any source file.
- Any workflow file.

Commands to run:

```powershell
$sha = git rev-parse HEAD
$short = $sha.Substring(0, 8)
$image = "ghcr.io/airyder/fincept/quant-foundry-training:$sha"

gh run list --workflow build-runpod-training.yml --limit 5

uv run python scripts/runpod_create_smoke_endpoint.py `
  --image-tag $image `
  --name "qf-training-layered-$short" `
  --template-name "qf-training-layered-$short-template" `
  --copy-registry-auth-from-endpoint-id $env:RUNPOD_REGISTRY_AUTH_SOURCE_ENDPOINT_ID `
  --workers-min 1 `
  --workers-max 1 `
  --container-disk-gb 20 `
  --docker-args "" `
  --idle-timeout 300 `
  --scaler-type QUEUE_DELAY `
  --scaler-value 4 `
  --gpu-ids ADA_24 `
  --wait-health `
  --wait-timeout 600 `
  --wait-interval 10 `
  --env "QUANT_FOUNDRY_CALLBACK_SECRET=$env:QUANT_FOUNDRY_CALLBACK_SECRET"
```

Evidence to collect:

- workflow run id and success
- exact image tag
- endpoint id
- redacted endpoint settings
- first healthy `/health` output

Acceptance criteria:

- Fresh endpoint exists.
- It references the exact SHA image.
- It has `workersMin=1`, `workersMax=1` only while testing.
- It reaches ready/idle with no unhealthy workers.

Rollback plan:

- Scale the endpoint down to `workersMin=0`, `workersMax=0`.

Common mistakes to avoid:

- Do not reuse a stale endpoint for image-level tests.
- Do not leave debug endpoints warm.
- Do not print registry auth ids or secrets.

## Card 4: Layer Probe Agent

Agent name / role:

- Layer Probe Agent

Objective:

- Run the live Layer 0 probe first. Only if it passes, run layers 1 through 5.

Context needed:

- Layer 0 is the deciding test.
- If Layer 0 fails, stop. Do not run layers 1 through 5.

Files they may edit:

- None.
- Optional receipts under `reports/runpod-test-runs/`.

Files they must not edit:

- All source files.
- All workflow files.

Commands to run:

```powershell
$endpoint = "<endpoint-id>"
$sha = git rev-parse HEAD
$short = $sha.Substring(0, 8)
$image = "ghcr.io/airyder/fincept/quant-foundry-training:$sha"

$payload = @{
  input = @{
    task = "callback_secret_canary"
    job_id = "qf:diag-layer0:$short:001"
    nonce = "layer0-nonce"
    diag_layer = 0
  }
} | ConvertTo-Json -Compress

uv run python scripts/runpod_smoke_probe.py `
  --endpoint-id $endpoint `
  --image-tag $image `
  --interval 5 `
  --timeout 180 `
  --payload-json $payload
```

If and only if Layer 0 passes:

```powershell
foreach ($layer in 1..5) {
  $payload = @{
    input = @{
      task = "callback_secret_canary"
      job_id = "qf:diag-layer$layer:$short:001"
      nonce = "layer$layer-nonce"
      diag_layer = $layer
    }
  } | ConvertTo-Json -Compress

  uv run python scripts/runpod_smoke_probe.py `
    --endpoint-id $endpoint `
    --image-tag $image `
    --interval 5 `
    --timeout 240 `
    --payload-json $payload
  if ($LASTEXITCODE -ne 0) { throw "layer $layer failed; stop" }
}
```

Evidence to collect:

- full JSONL probe output
- final status for each job
- health before, during, and after each job
- first failing layer, if any

Acceptance criteria:

- Layer 0 reaches `COMPLETED`, or a failure receipt proves the pivot condition.
- Layers 1 through 5 are run only after Layer 0 passes.

Rollback plan:

- No code rollback.
- Ask Cleanup Agent to scale endpoint down after receipts are captured.

Common mistakes to avoid:

- Do not skip Layer 0.
- Do not continue after Layer 0 fails.
- Do not treat local layer success as live success.

## Card 5: Evidence Collector

Agent name / role:

- Evidence Collector

Objective:

- Preserve the receipts in a structured way and write a short interpretation.

Context needed:

- Later agents must not have to infer what happened.
- Every claim must map to a receipt.

Files they may edit:

- `reports/runpod-test-runs/**`
- Optional: a short Markdown summary inside the same receipt directory

Files they must not edit:

- Source code
- Dockerfiles
- Workflow files
- Product docs outside the receipt directory unless explicitly asked

Commands to run:

```powershell
$sha = git rev-parse HEAD
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$receiptDir = "reports/runpod-test-runs/$sha/$stamp"
New-Item -ItemType Directory -Force $receiptDir | Out-Null
git status --short --branch | Tee-Object "$receiptDir/repo-state.txt"
git rev-parse HEAD | Tee-Object -Append "$receiptDir/repo-state.txt"
```

Evidence to collect:

- repo state
- Dockerfile diff or no-diff note
- local test outputs
- ruff output
- workflow id and result
- endpoint id
- redacted endpoint settings
- probe JSONL
- cleanup proof
- interpretation

Acceptance criteria:

- A new agent can read the receipt directory and understand pass/fail without repeating the test.

Rollback plan:

- No source rollback.
- If a receipt accidentally contains secrets, stop and ask the operator to rotate affected credentials before committing or sharing it.

Common mistakes to avoid:

- Do not paste secret values.
- Do not paste full callback signatures in shared summaries.
- Do not summarize without linking to exact raw receipt files.

## Card 6: Cleanup Agent

Agent name / role:

- Cleanup Agent

Objective:

- Ensure no debug endpoint is left warm unnecessarily.

Context needed:

- Debug endpoints should not keep `workersMin=1` after tests.
- Keep production/inference endpoints untouched.

Files they may edit:

- None.

Files they must not edit:

- All source files.
- All endpoint settings for existing production or inference endpoints unless explicitly named by the operator.

Commands to run:

```powershell
$endpoint = "<debug-endpoint-id>"
Invoke-RestMethod `
  -Method Post `
  -Uri "https://rest.runpod.io/v1/endpoints/$endpoint/update" `
  -Headers @{
    Authorization = "Bearer $env:RUNPOD_API_KEY"
    "Content-Type" = "application/json"
  } `
  -Body (@{ workersMin = 0; workersMax = 0 } | ConvertTo-Json -Compress) |
  ConvertTo-Json -Depth 8

Invoke-RestMethod `
  -Uri "https://api.runpod.ai/v2/$endpoint/health" `
  -Headers @{ Authorization = "Bearer $env:RUNPOD_API_KEY" } |
  ConvertTo-Json -Depth 8
```

Evidence to collect:

- endpoint id
- update response
- health response after scale-down

Acceptance criteria:

- Debug endpoint has no warm worker.
- No queued jobs remain unaddressed.
- Existing inference endpoint remains untouched.

Rollback plan:

- If the wrong endpoint was changed, immediately restore only that endpoint to its previous worker settings and report the incident.

Common mistakes to avoid:

- Do not scale down production or inference endpoints.
- Do not delete endpoints unless the operator explicitly says to delete.
- Do not leave `workersMin=1`.

## Card 7: Regression Guard Agent

Agent name / role:

- Regression Guard Agent

Objective:

- Add or propose a narrow guard that prevents reintroducing the failing healthcheck pattern after the root cause is proven.

Context needed:

- This card should run only after Test A passes or the team explicitly accepts no-healthcheck as the fix.

Files they may edit:

- `runpod/tests/**`
- `docs/runpod-fix-plan/**`
- Possibly `runpod/quant-foundry-training/README.md` if the team asks for doc drift cleanup

Files they must not edit:

- Handler logic
- Inference worker files
- App/UI files

Commands to run:

```powershell
Select-String -Path runpod/quant-foundry-training/Dockerfile -Pattern "HEALTHCHECK"
uv run ruff check runpod/quant-foundry-training scripts
```

Evidence to collect:

- test or check added
- command output proving it works

Acceptance criteria:

- Guard fails if an import-based Docker healthcheck is reintroduced.
- Guard does not block intentional future healthcheck work if the plan is updated with receipts.

Rollback plan:

- Revert only the guard commit if it blocks legitimate builds and the team decides to remove it.

Common mistakes to avoid:

- Do not overfit to comments.
- Do not block all uses of the word `HEALTHCHECK` in documentation.
- Do not add broad CI churn.

## Card 8: Final Acceptance Agent

Agent name / role:

- Final Acceptance Agent

Objective:

- Verify end-to-end acceptance and close the test sequence.

Context needed:

- Must read all receipts from Baseline Auditor, Endpoint Creator, Layer Probe, Evidence Collector, and Cleanup Agent.
- Must not accept "local passed" as live proof.

Files they may edit:

- Final receipt summary under `reports/runpod-test-runs/**`
- Optional final docs update if explicitly requested

Files they must not edit:

- Source code, unless a new explicit implementation task is opened.

Commands to run:

```powershell
git status --short --branch
gh run list --workflow build-runpod-training.yml --limit 5
```

Use the endpoint health/probe commands from `01-validation-baseline.md` if any receipt is missing.

Evidence to collect:

- exact accepted SHA
- exact accepted image tag
- endpoint id
- layer results
- canary result
- worker health after canary
- cleanup result

Acceptance criteria:

- All criteria in `05-acceptance-criteria.md` are checked or explicitly marked blocked with evidence.

Rollback plan:

- If final acceptance fails, leave a failing-layer or failing-boundary summary and scale down debug endpoints.

Common mistakes to avoid:

- Do not mark complete if jobs stayed `IN_QUEUE`.
- Do not mark complete if a debug endpoint is still warm.
- Do not claim product behavior changed; the fix must preserve the product workflow.

