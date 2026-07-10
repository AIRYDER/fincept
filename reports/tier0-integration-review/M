# Merge Order

## Branches to merge (in order)

1. **`tier0/durable-artifacts`** — handler.py changes (artifact deny gate, output_prefix validation). Merge first because it touches handler.py and other branches may conflict.
2. **`tier0/metric-sanity`** — runpod_training.py + handler.py (comment-only). Merge second — it builds on the handler.py state from #1 but only adds a comment to handler.py (logic is in runpod_training.py).
3. **`tier0/runpod-lifecycle-timeout`** — RunPod scripts + new lifecycle helper. Merge third — no handler.py changes, no conflicts with #1/#2.
4. **`tier0/image-slimming`** — new Dockerfile.slim + test file. Merge fourth — no conflicts with any prior branch (only touches Dockerfile.slim and a new test).
5. **`tier0/ruff-burndown`** — broad formatting/lint fixes across 272 files. Merge LAST — it must be rebased on top of all feature branches to avoid noisy conflicts. The formatting changes are whitespace-only and will not conflict semantically.

## Conflict Risk

| Pair | Risk | Reason |
|------|------|--------|
| durable-artifacts ↔ metric-sanity | LOW | Both touch handler.py but different areas (artifact: L184-296, L3493-3510; metric: comment-only at L3725). Logic for metric-sanity is in runpod_training.py. |
| durable-artifacts ↔ ruff-burndown | MEDIUM | Ruff reformats handler.py. Rebase ruff-burndown on top. |
| metric-sanity ↔ ruff-burndown | MEDIUM | Ruff reformats runpod_training.py. Rebase ruff-burndown on top. |
| runpod-lifecycle-timeout ↔ ruff-burndown | MEDIUM | Ruff reformats the RunPod scripts. Rebase ruff-burndown on top. |
| image-slimming ↔ ruff-burndown | LOW | Ruff may format the new test file. Rebase ruff-burndown on top. |

## Recommended Merge Strategy

```bash
# 1. Merge durable-artifacts into the target branch
git checkout fix/test-harness-optional-deps-guards  # or main
git merge tier0/durable-artifacts --no-ff

# 2. Merge metric-sanity
git merge tier0/metric-sanity --no-ff

# 3. Merge runpod-lifecycle-timeout
git merge tier0/runpod-lifecycle-timeout --no-ff

# 4. Merge image-slimming
git merge tier0/image-slimming --no-ff

# 5. Rebase ruff-burndown on top, then merge
git checkout tier0/ruff-burndown
git rebase fix/test-harness-optional-deps-guards
git checkout fix/test-harness-optional-deps-guards
git merge tier0/ruff-burndown --no-ff
```
