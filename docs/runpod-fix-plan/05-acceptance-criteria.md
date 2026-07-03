# RunPod Fix Plan: Acceptance Criteria

Last updated: 2026-07-03

Final acceptance requires live RunPod evidence. Local tests, static checks, and successful image builds are necessary supporting evidence, not final proof.

## Required Final Success Criteria

Use this checklist for final acceptance:

- [ ] Smoke worker still completes a live RunPod job.
- [ ] Training layered endpoint Layer 0 completes live.
- [ ] Layers 1 through 5 complete live, or the first failing layer is isolated with receipts.
- [ ] Full canary path completes live.
- [ ] Worker remains healthy after canary completion.
- [ ] Job reaches a terminal status instead of staying `IN_QUEUE`.
- [ ] No debug endpoint is left with `workersMin=1`.
- [ ] No API keys or callback secrets are printed in receipts.
- [ ] Registry auth ids are redacted in shared summaries.
- [ ] Callback signatures are not printed except in local-only evidence where explicitly required.
- [ ] Build workflow produces an exact SHA-tagged image.
- [ ] Exact accepted image tag is recorded.
- [ ] Existing inference endpoint remains untouched.
- [ ] Fincept / Quant Foundry product use cases remain unchanged.
- [ ] RunPod training-worker architecture remains intact.
- [ ] Callback-secret canary use case remains intact.
- [ ] No unrelated UI, app, product identity, or user journey changes were made.

## Minimum Evidence Bundle

The final evidence bundle must include:

- branch name
- accepted commit SHA
- accepted image tag
- GitHub Actions training build run id
- smoke worker image tag and endpoint id
- training endpoint id
- redacted endpoint settings
- `/health` before Layer 0
- `/run` response for Layer 0
- `/status` sequence for Layer 0 through terminal status
- `/health` after Layer 0
- layer 1 through 5 receipts, if Layer 0 passed
- full canary receipt
- cleanup receipt showing debug endpoint scale-down
- short interpretation naming the proven root cause or the still-open failing boundary

## Layer Acceptance

Layer 0 accepted only if:

- `/run` returns a job id
- `/status/{job_id}` reaches `COMPLETED`
- output includes `diag_layer=0`
- output is JSON-serializable
- worker health remains acceptable after completion

Layers 1 through 5 accepted only if:

- each layer reaches `COMPLETED`
- each layer output matches the expected layer label
- the worker remains healthy between jobs

If a layer fails:

- stop at the first failing layer
- capture probe JSONL
- capture health before/during/after
- record whether the job stayed `IN_QUEUE`, failed terminally, timed out, or caused pod exit
- open the next narrow task for that layer only

## Canary Acceptance

Full callback-secret canary accepted only if:

- live job reaches `COMPLETED`
- response includes the expected canary payload fields
- callback signature is present but not printed in shared summaries
- signature verification can be demonstrated without exposing `QUANT_FOUNDRY_CALLBACK_SECRET`
- worker remains healthy after the job

## Security Acceptance

Security acceptance requires:

- no secrets in command output committed to the repo
- no GraphQL or REST URLs with `?api_key=`
- all RunPod API calls use `Authorization: Bearer $env:RUNPOD_API_KEY`
- no broker credentials added to the worker image
- no Redis, DB write URL, Alpaca, broker, or trading env vars added to the worker
- callback-secret canary remains fail-closed
- inference endpoint remains untouched

If any helper script prints secrets or embeds API keys in URLs, mark it as a bug and open a separate narrow fix task. Do not quietly normalize the unsafe behavior.

## Product Preservation Acceptance

The fix is acceptable only if it preserves:

- the Quant Foundry training-worker use case
- the smoke worker as a diagnostic baseline
- the canary/callback-secret validation workflow
- later real training workloads
- signed callback contract
- human-gated promotion model
- existing inference worker behavior
- current app/product/UI design

The fix is not acceptable if it:

- replaces training with smoke-only behavior
- removes canary signing
- removes preflight without a separate security review
- changes unrelated user journeys
- broadens worker authority with trading or broker credentials
- leaves debug infrastructure running

## Rollback Criteria

Rollback the no-healthcheck implementation only if:

- Test A proves Layer 0 still fails with the same symptoms and the team decides the no-healthcheck change is not useful, or
- a new receipt proves no-healthcheck causes a separate production-blocking regression.

Do not rollback just because a later layer fails. Later layer failures should create narrower tasks.

Rollback steps:

```powershell
git status --short --branch
git show --stat HEAD
# If and only if HEAD is the no-healthcheck commit and rollback is approved:
git revert HEAD
git push
```

After rollback:

- wait for image workflow if a reverted image is needed
- scale down debug endpoints
- record rollback SHA and reason

## Stop Conditions

Stop and ask for operator direction if:

- target branch/SHA cannot be identified
- current Dockerfile is not the failed layered shape and the operator has not said whether to test current HEAD or branch from `412080c6`
- a live endpoint would require exposing secrets
- a command would modify production or inference endpoint settings
- RunPod API behavior differs from the documented command shape

Otherwise, keep the loop narrow: exact SHA, one variable, fresh endpoint, Layer 0 first, receipts, cleanup.

