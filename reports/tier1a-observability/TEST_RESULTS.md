# Test Results — Observability & Cost Tracking

## New tests (test_cost_tracker.py)

- **44/44 passed**
- 0 failed
- 0 errors

### Test breakdown:
- TestRecordJobDispatch: job creation, idempotency, fields, GPU type/count, container image, payload ref
- TestUpdateJobStatus: status transitions, timestamps
- TestLinkCallback: callback receipt linking
- TestRecordCostEvent: amount * unit_cost = total_cost, metadata, event_id return
- TestRecordMetric: metric recording, units
- TestComputeJobCost: sum of events
- TestComputePeriodCost: rollup upsert, multiple jobs
- TestEstimateGpuCost: each GPU type (RTX_4090, A100_80GB, A100_40GB, L4, default)
- TestCheckConstraints: bad status, bad event_type, bad metric_type, negative cost
- TestIdempotency: ON CONFLICT DO NOTHING for training_jobs
- TestNoSecrets: no secrets in DB, payload_ref is a file path
- TestListFilter: list by status, by model_family

## Regression tests
- test_schemas.py: passed
- test_runpod_client.py: passed
- test_promotion.py: passed
- test_dossier.py: passed

## Notes
- pytest exits code 1 on Windows due to temp-dir cleanup PermissionError — all test dots are green
- Python 3.12.9 (uv cpython-3.12.9-windows-x86_64)
