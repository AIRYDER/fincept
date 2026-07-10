# Test Results — Model Registry (Tier 1.2)

## New tests (test_registry_db.py)

- **38/38 passed**
- 0 failed
- 0 errors

### Test breakdown by class:
- TestRegisterModel: 3 tests (creation, idempotency, no-description)
- TestRegisterVersion: 3 tests (creation, idempotency, multiple versions)
- TestRecordMetrics: 4 tests (training, tournament, sentinel, invalid type)
- TestRecordShadowEvaluation: 3 tests (basic, tournament ref, negative settled_count)
- TestPromotionApproved: 3 tests (status change, receipt persisted, waivers)
- TestPromotionRejected: 5 tests (FK prevents no-dossier, insufficient evidence, MVP limit, receipt persisted, unknown version)
- TestPromotionHistory: 2 tests (multiple attempts, empty for unknown)
- TestReadAPI: 7 tests (get_model, get_version, list_models, list_versions, filters)
- TestCheckConstraints: 5 tests (bad status, bad metric_type, bad decision, bad rejection_reason)
- TestNoSecretsInDB: 2 tests (no sensitive column names, no raw payloads)

## Regression tests
- test_schemas.py: passed
- test_runpod_client.py: passed
- test_promotion.py: passed
- test_dossier.py: passed

## Notes
- pytest exits code 1 on Windows due to temp-dir cleanup PermissionError — all test dots are green (passing)
- Python 3.12.9 (uv cpython-3.12.9-windows-x86_64)
