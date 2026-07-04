# Commands Run

## Test execution (Python 3.12.9 via service venv)

```bash
# New dispatch wiring tests (18 tests)
cd services/quant_foundry
./.venv/Scripts/python.exe -m pytest tests/test_runpod_dispatch.py -v

# Existing client + schema regression (37 tests)
./.venv/Scripts/python.exe -m pytest tests/test_runpod_client.py tests/test_schemas.py -v

# Broader regression sweep (93 tests)
./.venv/Scripts/python.exe -m pytest \
  tests/test_gateway_runpod_loop.py \
  tests/test_runpod_connection_hardening.py \
  tests/test_shadow_dispatch.py \
  tests/test_artifact_writer.py \
  -q -p no:cacheprovider
```

## Notes
- The service venv is at `services/quant_foundry/.venv` (Python 3.12.9).
- The system Python is 3.10.6 which does not meet the `>=3.12`
  requirement in pyproject.toml; the service venv must be used.
- pytest exits with code 1 on Windows due to a `PermissionError` in
  pytest's temp-dir cleanup (`cleanup_dead_symlinks`), NOT due to test
  failures. All test dots show green (passing).
