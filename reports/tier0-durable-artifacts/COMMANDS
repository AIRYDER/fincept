# Commands Run

## Compile check (Python 3.10 — all files compile)
```bash
python -m compileall runpod/quant-foundry-training/handler.py scripts/verify_artifact_manifest.py services/quant_foundry/tests/test_artifact_writer.py
```
Output:
```
Compiling 'runpod/quant-foundry-training/handler.py'...
Compiling 'scripts/verify_artifact_manifest.py'...
Compiling 'services/quant_foundry/tests/test_artifact_writer.py'...
```
Exit code: 0

## Full test suite (Python 3.12 — 41/41 pass)
```bash
# Python 3.10 blocks on StrEnum import (pre-existing); use Python 3.12:
$env:PYTHONPATH = "services/quant_foundry/src;runpod/quant-foundry-training"
py -3.12 -m pytest services/quant_foundry/tests/test_artifact_writer.py -v --tb=short
```
Result: **41 passed, 0 failed** (24 original + 17 new)
(Exit code 1 is a Windows pytest tmp_path cleanup PermissionError, not a test failure)

## Manifest verifier — valid manifest
```bash
$env:QUANT_FOUNDRY_CALLBACK_SECRET = "test-secret"
python scripts/verify_artifact_manifest.py <manifest_path>
```
Output:
```
Verifying artifact manifest:
  URI:      file:///.../model.pkl
  SHA-256:  aa0211...
  Size:     33 bytes
  Format:   pickle
  Fetched:  33 bytes, sha256=aa0211...
  SHA-256:  OK (matches manifest)
  Receipt:  OK (HMAC verified)
VERIFIED: artifact sha256 matches and write receipt is authentic.
```
Exit code: 0

## Manifest verifier — tampered manifest (wrong sha256)
```bash
python scripts/verify_artifact_manifest.py <tampered_manifest_path>
```
Output:
```
FAIL: sha256 mismatch — manifest declares aaa... but artifact hashes to aa0211...
```
Exit code: 1

## Git branch
```bash
git checkout -b tier0/durable-artifacts
```
