# Commands Run

## Validation

```bash
# Compile-check changed source files (Python 3.10 default interpreter)
python -m compileall services/quant_foundry/src/quant_foundry/runpod_training.py
python -m compileall runpod/quant-foundry-training/handler.py
python -m compileall services/quant_foundry/tests/test_metric_sanity.py
```

All three compiled with exit code 0.

## Tests

```bash
# Run the new metric sanity test suite (Python 3.12 + PYTHONPATH=src)
cd services/quant_foundry
$env:PYTHONPATH="src"
py -3.12 -m pytest tests/test_metric_sanity.py -x -q
# -> 18 passed in 0.62s

# Regression: existing runpod_training + runpod_modes tests
py -3.12 -m pytest tests/test_runpod_training.py tests/test_runpod_modes.py -q
# -> 67 passed in 0.82s
```

## Git

```bash
git checkout -b tier0/metric-sanity
```

## Notes

- The default `python` on this machine is Python 3.10, which cannot
  import the `quant_foundry` package (it uses `enum.StrEnum`, added in
  3.11). `compileall` was used as the primary validation on 3.10.
- Full pytest execution used Python 3.12 (`py -3.12`) with
  `PYTHONPATH=src` since the package is not pip-installed in this
  environment.
