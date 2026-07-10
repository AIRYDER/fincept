# Mypy Triage Report — C4B CI Greening

## Total Errors (before fixes)
190 mypy errors across 52 files

## Final Result
**0 errors across 162 source files** — mypy passes clean.

## Categories

| Category | Count | Strategy |
|----------|-------|----------|
| `misc` (torch nn.Module subclass) | 11 | `# type: ignore[misc]` — torch resolves to Any when not installed |
| `arg-type` | 8+ | `cast()`, precise annotations, `# type: ignore[arg-type]` |
| `attr-defined` | 5 | `# type: ignore[attr-defined]`, `assert`, `Any` type |
| `union-attr` (None safety) | 3 | `assert self.x is not None` guards |
| `no-any-return` | 1 | `# type: ignore[no-any-return]` |
| `name-defined` | 1 | Replaced invalid cast with type: ignore |
| `unused-ignore` | 3 | Removed or corrected error codes |
| Protocol mismatch | 2 | Changed Protocol attrs to `@property` |

## Fixes Applied

### gateway.py
- Explicit type annotations for `shadow_ledger` and `dossier_store` attributes
- `cast("str", outbox_status)` for status_map.get()
- `assert self._registry is not None` before register_model

### bundle_io.py
- `assert self.bundle.meta_model is not None` in `_meta_predict`

### training_manifest.py
- Replaced invalid `cast("FamilyValidationResult | None", ...)` with `# type: ignore[no-any-return]`

### real_trainer.py
- `from collections.abc import Callable` import
- `cast("list[Callable[..., Any]] | None", callbacks)` for lgb.train() callbacks
- Fixed `label_map` type: `dict[int, int]` → `dict[str, int]` via dict comprehension
- None-safety asserts for `column_roles`, `task_spec`, `fold_spec`
- `cast("Any", ...)` for numpy array operations

### finbert.py
- `assert self._model is not None` and `assert self._tokenizer is not None` guards

### registry.py
- `# type: ignore[attr-defined]` for dynamic `cls.info` attribute

### registry_db.py
- `# type: ignore[attr-defined]` for `result.rowcount` (SQLAlchemy stubs)
- Changed `model: type` to `model: Any` in update functions

### verification_matrix.py
- `# type: ignore[arg-type]` for `EventRecord(**common)` dict unpacking

### tabpfn_adapter.py
- Changed `rows: list[list[float]]` to `rows: list[tuple[float, ...]]`

### optuna_tuning.py
- `cast("str", study_data["created_at"])` for StudyArtifact

### calibration.py
- Renamed `X`/`y` to `X_iso`/`y_iso` in IsotonicRegression branch

### windowed_tensor_builder.py
- `# type: ignore[arg-type]` for `np.savez()` kwargs

### pit_evidence.py
- Changed `_ManifestLike` Protocol: settable vars → `@property`
- Changed `_FeatureRowLike` Protocol: settable vars → `@property`

### Torch nn.Module subclass files (7 files)
- `# type: ignore[misc]` for all nn.Module subclass definitions
- Files: graph_runtime.py, tft_trainer.py, patchtst_trainer.py, lob_trainer.py, event_trainer.py, graph_ranker.py, tabm_trainer.py

## Remaining Errors
**0** — mypy passes clean with `uv run mypy services/quant_foundry/src`

## Verification
- mypy: 0 errors across 162 source files
- C1-C3 tests: 187 passed, 2 skipped, 0 failed
- No behavior changes (typing annotations, casts, asserts, and targeted ignores only)
