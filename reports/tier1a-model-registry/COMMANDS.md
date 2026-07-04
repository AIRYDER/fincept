# Commands — Model Registry (Tier 1.2)

## Run tests
```powershell
cd C:\Users\nolan\CascadeProjects\fincept-terminal\services\quant_foundry
& ".venv/Scripts/python.exe" -m pytest tests/test_registry_db.py -v
```

## Run with regression tests
```powershell
& ".venv/Scripts/python.exe" -m pytest tests/test_registry_db.py tests/test_promotion.py tests/test_dossier.py tests/test_schemas.py -v
```

## Run all Tier 1A tests
```powershell
& ".venv/Scripts/python.exe" -m pytest tests/test_registry_db.py tests/test_cost_tracker.py tests/test_callback_db_sinks.py tests/test_runpod_dispatch.py -v
```
