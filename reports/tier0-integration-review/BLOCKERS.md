# Blockers

## No hard blockers

All Tier 0 tasks completed. No blocking issues prevent merge.

## Soft blockers (should be addressed post-merge)

### B1: Docker build not verified
- **Issue:** Docker is not available locally. Neither the production Dockerfile nor Dockerfile.slim was built.
- **Impact:** Unknown whether the slim image actually builds or whether handler.py imports torch at module load time (would crash the slim image).
- **Recommendation:** Build both images in a Docker-enabled environment before deploying. Run a live canary against the slim image with operator approval.

### B2: Python 3.10 vs 3.12 test gap
- **Issue:** Local Python is 3.10.6; project requires >=3.12. Most pytest runs fail on import of 3.12-only syntax (StrEnum, datetime.UTC).
- **Impact:** Artifact writer tests (41) and metric sanity tests (18) were verified by workers but could not be re-verified by the integration reviewer locally.
- **Recommendation:** Run full pytest in a Python 3.12 environment before merge.

### B3: Ruff burn-down incomplete (880 errors remain)
- **Issue:** 880 ruff errors remain after auto-fix (52.3% reduction from 1845).
- **Impact:** CI will still show errors. The remaining errors require manual fixes (B017, T201, F841, etc.).
- **Recommendation:** Schedule a follow-up ruff burn-down task for the remaining 880 errors. Do not mix with feature work.

### B4: executionTimeout field name unverified
- **Issue:** The `executionTimeout` field name in the RunPod GraphQL `EndpointInput` schema is based on documentation, not live testing.
- **Impact:** If the field name is wrong, the endpoint creation may silently ignore it.
- **Recommendation:** Verify with a live canary (operator approval required) that the endpoint accepts `executionTimeout` and the timeout is enforced.

### B5: Artifact deny gate fires after training, not before
- **Issue:** Builder 3's /tmp deny gate fires at the writer selection block (after training completes), not before training starts.
- **Impact:** GPU time is wasted on a job whose artifact will be rejected. The signed failure envelope still fires correctly, but the operator pays for training that produces no artifact.
- **Recommendation:** Move the gate before training in a follow-up task (requires touching handler.py L3237-3283, which was outside Builder 3's owned range).
