# Test Results

## New DB sink tests (test_callback_db_sinks.py)

```
============================= 31 passed in 1.03s ==============================
```

### Test breakdown

**TestDbDossierStore** (6 tests)
- test_store_inserts_dossier_and_artifact PASSED
- test_idempotent_insert_same_content_hash PASSED
- test_different_model_creates_new_row PASSED
- test_artifact_manifest_id_mismatch_raises PASSED
- test_no_secrets_in_dossier_row PASSED
- test_get_and_list PASSED

**TestDbShadowLedgerStore** (6 tests)
- test_store_inserts_predictions PASSED
- test_idempotent_insert_same_prediction_id PASSED
- test_non_shadow_authority_rejected PASSED
- test_db_check_constraint_rejects_non_shadow PASSED
- test_no_secrets_in_prediction_row PASSED
- test_empty_predictions_noop PASSED

**TestCallbackReceiptDbStore** (4 tests)
- test_write_inserts_receipt PASSED
- test_idempotent_write_same_callback_id PASSED
- test_no_secrets_in_receipt_row PASSED
- test_get_by_job_id PASSED

**TestCallbackDlqDbStore** (4 tests)
- test_write_inserts_dlq PASSED
- test_idempotent_write_same_idempotency_key PASSED
- test_no_secrets_in_dlq_row PASSED
- test_count PASSED

**TestCallbackMetricsDbStore** (6 tests)
- test_record_inserts_event PASSED
- test_invalid_event_raises PASSED
- test_idempotent_record_same_ts_and_event PASSED
- test_rejection_rate PASSED
- test_rejection_rate_empty PASSED
- test_no_secrets_in_metrics_row PASSED

**TestCallbackProcessorWithDbSinks** (4 tests)
- test_inference_callback_with_db_shadow_ledger PASSED
- test_training_callback_with_db_dossier_store PASSED
- test_replayed_callback_idempotent_in_db PASSED
- test_bad_signature_no_db_write PASSED

**TestTamperDetection** (1 test)
- test_same_job_different_payload_raises PASSED

## Existing callback tests (regression)

```
============================= 110 passed in 1.69s =============================
```

Test files run:
- test_signatures.py
- test_inbox.py
- test_callback_dlq.py
- test_callback_metrics.py
- test_gateway_callbacks.py
- test_shadow_ledger.py
- test_dossier.py

## fincept-db tests (regression)

```
============================= 59 skipped in 0.66s ==============================
```

All DB-gated tests skip cleanly (no Postgres at :5432). No failures.

## Total

- **31 new tests passed**
- **110 existing tests passed** (no regressions)
- **59 fincept-db tests skipped** (no Postgres available)
- **0 failures**
