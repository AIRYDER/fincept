# Commands — Observability & Cost Tracking

## Run tests
```powershell
cd C:\Users\nolan\CascadeProjects\fincept-terminal\services\quant_foundry
& ".venv/Scripts/python.exe" -m pytest tests/test_cost_tracker.py -v
```

## Run with regression tests
```powershell
& ".venv/Scripts/python.exe" -m pytest tests/test_cost_tracker.py tests/test_runpod_client.py tests/test_schemas.py -v
```
