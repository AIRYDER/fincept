# Test Results

## Summary

| Metric | Value |
|--------|-------|
| Total tests | 41 |
| Passed | 41 |
| Failed | 0 |
| Errors | 0 |
| New tests added | 17 |
| Original tests | 24 |
| Original tests still passing | 24/24 |

## Test runner

- **Python 3.10** (system default): Cannot import handler module — `StrEnum`
  not available until Python 3.11. This is a pre-existing issue affecting ALL
  tests in the file, not caused by this change. `compileall` passes on 3.10.
- **Python 3.12** (`py -3.12`): All 41 tests pass.

## Command
```bash
$env:PYTHONPATH = "services/quant_foundry/src;runpod/quant-foundry-training"
py -3.12 -m pytest services/quant_foundry/tests/test_artifact_writer.py -v --tb=short
```

## Full test list (all PASSED)

### Original 24 tests (unchanged, still passing)
1. test_artifact_write_result_is_frozen
2. test_artifact_write_result_rejects_unknown_fields
3. test_write_receipt_verifies
4. test_allowed_uri_schemes_accepted
5. test_disallowed_uri_scheme_rejected
6. test_empty_uri_rejected
7. test_fake_writer_computes_expected_sha
8. test_fake_writer_rejects_empty_bytes
9. test_runpod_real_trainer_routes_catboost_gpu
10. test_runpod_real_trainer_routes_xgboost_gpu
11. test_runpod_real_trainer_rejects_unrouted_family
12. test_runpod_tree_gpu_family_requires_roles
13. test_volume_writer_writes_and_verifies
14. test_volume_writer_detects_sha_mismatch
15. test_volume_writer_rejects_empty_bytes
16. test_presigned_writer_rejects_http_scheme
17. test_presigned_writer_rejects_ftp_scheme
18. test_presigned_writer_uploads_via_put
19. test_presigned_writer_fails_on_non_200
20. test_presigned_writer_fails_on_network_error
21. test_build_artifact_write_failure_callback_is_signed
22. test_handler_rejects_disallowed_presigned_uri_scheme
23. test_handler_fake_writer_canary_no_persistence
24. test_handler_volume_writer_persists_artifact

### New 17 tests (durable artifact deny gate + validation)
25. test_is_under_tmp_detects_tmp_paths
26. test_validate_output_prefix_denies_tmp_for_real_jobs
27. test_validate_output_prefix_denies_tmp_for_research_mode
28. test_validate_output_prefix_allows_tmp_for_canary
29. test_validate_output_prefix_rejects_invalid_prefix
30. test_validate_output_prefix_accepts_runpod_volume
31. test_validate_output_prefix_accepts_workspace
32. test_validate_output_prefix_accepts_presigned_url
33. test_validate_output_prefix_accepts_s3_uri
34. test_validate_output_prefix_rejects_file_uri_to_tmp
35. test_validate_output_prefix_accepts_file_uri_to_volume
36. test_validate_output_prefix_rejects_no_destination_for_real_job
37. test_validate_output_prefix_rejects_file_uri_to_non_volume
38. test_handler_denies_tmp_for_real_jobs
39. test_handler_allows_tmp_for_canary_jobs
40. test_handler_denies_invalid_prefix_for_real_jobs
41. test_handler_denies_no_destination_for_real_jobs

## Manifest verifier tests (manual)

- Valid manifest (correct sha256 + receipt) → exit code 0, "VERIFIED"
- Tampered manifest (wrong sha256) → exit code 1, "FAIL: sha256 mismatch"
- Missing QUANT_FOUNDRY_CALLBACK_SECRET → exit code 2, "fail closed"
