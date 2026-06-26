# Code Style & Conventions Audit — ml-dataset-evidence-spine

**Scope:** Changes from commit `7dc5fc1` to `HEAD` (19 commits, 44 files, +7149/-194 lines).
**Reviewer focus:** Code style, ruff/mypy compliance, Pydantic v2 patterns, error handling, logging, dead code, formatting.
**Date:** 2026-06-26

---

## Summary

The `ml-dataset-evidence-spine` implementation is **stylistically excellent** and adheres closely to the project's established conventions. The new `fincept_core.datasets` package (`approved_roots.py`, `schemas.py`, `settlement.py`, `feature_snapshot.py`, `dossier.py`, `cv.py`, `__init__.py`) is a near-perfect stylistic clone of the baseline `fincept_core.prediction_log` module — same docstring structure (module-level "Why this module exists" / "Filesystem layout" / "Design constraints" sections), same section-separator comment style (`# ---...--- #`), same frozen-dataclass / frozen-Pydantic patterns, same append-only JSONL store shape, same tolerance-on-malformed-line read pattern.

**Ruff: PASS** — `ruff check` on all 8 target paths returns "All checks passed!" with zero warnings.

**Mypy (core datasets + settlements): PASS** — `mypy libs/fincept-core/src/fincept_core/datasets services/settlements` returns "Success: no issues found in 9 source files".

**Mypy (new services code if checked): FAIL** — 6 mypy errors found in `logreg.py`, `paper_spine_replay.py`, and `models.py` when those files are explicitly checked. These files are not in the mypy-checked surface for the core datasets/settlements scope, but they would fail if added to a mypy CI step.

**Overall verdict: PASS with minor findings.** The implementation demonstrates strong convention discipline. The issues below are minor / cosmetic and do not affect functionality.

---

## Findings

### 1. CONSISTENCY ISSUE — Inconsistent approved-roots error handling between `models.py` and `backtest.py`

**Files:**
- `services/api/src/api/routes/models.py:518-527` (`post_train`)
- `services/api/src/api/routes/backtest.py:128` (`post_run`)

`backtest.py` lets `ApprovedRootsError` propagate to the shared exception handler registered in `api/main.py`:
```python
approved_roots.resolve(body.bars_path)  # propagates -> shared handler -> 422
```

`models.py` catches it inline and returns a `JSONResponse` directly:
```python
try:
    _get_approved_roots().resolve(body.input_path)
except ApprovedRootsError as exc:
    return JSONResponse(status_code=422, content={...})
```

Both produce the same 422 body, but the inline catch in `models.py`:
- Duplicates the handler logic that `api/approved_roots.py` already centralizes.
- Causes a **mypy error** (see finding #3): the function is annotated `-> dict[str, Any]` but returns `JSONResponse` on this path.

**Recommendation:** Remove the inline try/except in `post_train` and let the error propagate to the shared handler, matching `backtest.py`.

---

### 2. STYLE VIOLATION — Mypy errors in new services code (6 errors)

**Command:** `uv run mypy services/agents/src/agents/baselines/logreg.py scripts/paper_spine_replay.py services/api/src/api/routes/models.py`

**Errors:**

| File | Line | Error |
|------|------|-------|
| `services/agents/src/agents/baselines/logreg.py` | 22 | `no-any-return`: Returning `Any` from function declared to return `ndarray` (`_sigmoid`) |
| `services/agents/src/agents/baselines/logreg.py` | 41 | `no-any-return`: Returning `Any` (`decision_function`) |
| `services/agents/src/agents/baselines/logreg.py` | 119 | `no-any-return`: Returning `Any` (`roc_auc`) |
| `scripts/paper_spine_replay.py` | 313 | `no-any-return`: Returning `Any` from function declared to return `dict[str, Any]` (`run_replay`) |
| `scripts/paper_spine_replay.py` | 497 | `no-any-return`: Returning `Any` (`run_settlement_proof`) |
| `services/api/src/api/routes/models.py` | 521 | `return-value`: Incompatible return (got `JSONResponse`, expected `dict[str, Any]`) |

The project runs mypy strictly (`mypy>=1.13` in every `pyproject.toml`). The `logreg.py` errors stem from numpy operations returning `Any` (e.g., `1.0 / (1.0 + np.exp(-z))`); wrapping with `np.asarray(..., dtype=float)` or casting would fix them. The `paper_spine_replay.py` errors stem from `to_jsonable()` returning `Any` being returned from functions annotated `-> dict[str, Any]`; a `cast(dict[str, Any], ...)` or explicit annotation on `to_jsonable` would fix them. The `models.py` error is the same issue as finding #1.

**Severity:** These files are not in the mypy-checked surface for the datasets/settlements scope, but they would fail a broader mypy CI step. The `logreg.py` and `paper_spine_replay.py` errors are `no-any-return` (strictness), not correctness bugs.

---

### 3. CONSISTENCY ISSUE — `_validate_agent_id` duplicated across 3 modules (documented as intentional)

**Files:**
- `libs/fincept-core/src/fincept_core/prediction_log.py:69-78` (original)
- `libs/fincept-core/src/fincept_core/datasets/settlement.py:73-87` (copy)
- `libs/fincept-core/src/fincept_core/datasets/feature_snapshot.py:66-81` (copy)

The `_BAD_NAME_CHARS` set and `_validate_agent_id` function are copied verbatim into `settlement.py` and `feature_snapshot.py`. Each copy includes a docstring/comment explicitly stating the duplication is intentional ("a future audit grep must find the identical forbidden-character set in every store so they stay symmetric").

This is a documented design decision, not an accident. However, from a pure code-style perspective, a shared `_validate_agent_id` in a common utility module (e.g., `fincept_core.datasets._path_utils`) would eliminate the maintenance risk of the three copies drifting. The `main.py` agent already imports it from `prediction_log`, proving cross-module import is viable.

**Severity:** Low — the duplication is documented and the audit-grep rationale is reasonable for a security-sensitive allow-list.

---

### 4. CONSISTENCY ISSUE — `WalkForwardWindow.to_dict()` manually constructs dict instead of using `model_dump()`

**File:** `libs/fincept-core/src/fincept_core/datasets/cv.py:174-183`

```python
class WalkForwardWindow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    ...
    def to_dict(self) -> dict[str, int]:
        return {
            "train_start": self.train_start,
            ...
        }
```

The sibling `Fold` class in the same module has no `to_dict()` and relies on `model_dump()` via the `fold_iter_to_dicts` helper. The rest of the new Pydantic code uses `model_dump()` / `model_dump_json()` / `model_validate()` consistently. The `to_dict()` method here is a port of the original `training_manifest.WalkForwardWindow.to_dict()` and is kept for API parity, but it's a minor inconsistency with the Pydantic v2 idiom.

**Recommendation:** Replace `to_dict()` body with `return self.model_dump()` (return type would become `dict[str, int]` which is compatible), or deprecate `to_dict()` in favor of `model_dump()`.

---

### 5. MINOR — `baselines/__init__.py` missing `from __future__ import annotations`

**File:** `services/agents/src/agents/baselines/__init__.py:1-15`

The new `baselines/__init__.py` does not include `from __future__ import annotations`, while the other new `__init__.py` files (`datasets/__init__.py`, `settlements/__init__.py`) and the existing `quant_foundry/__init__.py` all include it. The project convention is to include it in every module.

**Severity:** Cosmetic — no functional impact since the `__init__.py` only imports names, but inconsistent with the project-wide pattern.

---

### 6. MINOR — Line-length violations in `train.py` and `paper_spine_replay.py` (E501 not enforced)

**Files:**
- `services/agents/src/agents/gbm_predictor/train.py:177` (95 chars), `:218` (93 chars), `:477` (91 chars)
- `scripts/paper_spine_replay.py:439` (105 chars), `:442` (101 chars), `:515` (104 chars)

The project's ruff config (`[tool.ruff.lint]` in root `pyproject.toml`) does not include E501 in the select set (only `ignore = ["B008"]`), so `ruff check` passes. However, ruff's default line-length is 88, and these lines exceed it. The `paper_spine_replay.py` lines exceed even 100 chars.

**Severity:** Cosmetic — E501 is not enforced by the project's ruff config, so this is not a linting violation. Noted for formatting consistency.

---

### 7. MINOR — `LogRegBaseline` dataclass not frozen

**File:** `services/agents/src/agents/baselines/logreg.py:25-33`

```python
@dataclass
class LogRegBaseline:
    weights: np.ndarray
    bias: float
    n_features: int
    n_iter: int = 0
    loss_history: list[float] = field(default_factory=list)
```

The project convention (in `prediction_log.py`, `features.py`) is `@dataclasses.dataclass(frozen=True)`. `LogRegBaseline` uses a mutable `@dataclass` (no `frozen=True`). This is defensible — the class holds a numpy array and a list, and is meant to be a mutable picklable model object — but it's a minor deviation from the frozen-dataclass convention used elsewhere.

**Severity:** Cosmetic — the mutability is intentional for a model object.

---

### 8. MINOR — Import ordering: `settlements.worker` grouped with third-party in `paper_spine_replay.py`

**File:** `scripts/paper_spine_replay.py:18-31`

```python
import fakeredis.aioredis
from settlements.worker import tick_sync          # local service, grouped with third-party

from fincept_core.config import Settings          # local lib
from fincept_core.datasets import SettlementStore
...
from oms.paper import PaperFiller                 # local service
```

`settlements` is a workspace member (local package), but `from settlements.worker import tick_sync` is placed in the third-party import group (after `fakeredis`) with a blank line separating it from the `fincept_core` local imports. Ruff's isort rules did not flag this (I rules are not in the default select set), but it's a minor grouping inconsistency.

**Severity:** Cosmetic — ruff passes, no functional impact.

---

### 9. PASS — Pydantic v2 patterns

All new Pydantic models in `datasets/schemas.py`, `datasets/settlement.py`, `datasets/cv.py` use:
- `ConfigDict(frozen=True, extra="forbid")` consistently (every model).
- `field_validator` with `@classmethod` decorator correctly.
- `model_validator(mode="after")` for cross-field validation (`_no_lookahead`, `_decision_window_ordering`).
- `model_dump()`, `model_dump_json()`, `model_validate()`, `model_validate_json()` — no deprecated `dict()` / `parse_obj()` calls found anywhere in the new code.
- `FeatureSnapshot.model_dump()` used in `feature_snapshot.py:99` for serialisation.
- `SettlementRecord.model_dump_json()` / `model_validate_json()` used in `settlement.py:166,192`.

**Verdict:** Fully compliant with Pydantic v2 idioms.

---

### 10. PASS — Error handling style

- `ApprovedRootsError(ValueError)` with `code` attribute (`outside_root`, `traversal`, `symlink_escape`, `no_roots`) — matches the existing pattern of specific exception subclasses with machine-readable `code` attributes.
- `SettlementError(ValueError)` with `code` attribute (`look_ahead`, `duplicate`, `invalid_prediction_id`, `missing_settled_at`) — same pattern.
- No overly broad `try/except` in the core modules. The `except (json.JSONDecodeError, KeyError, ValueError)` pattern for malformed-line tolerance matches `prediction_log.py:282-286` exactly.
- The `except Exception as exc: # noqa: BLE001` in `main.py:602` is the one broad catch, but it's explicitly documented as best-effort for the feature-health sidecar and suppressed with a `log.warning`. This is the correct pattern for a non-critical side effect.
- `callback_metrics.py` raises plain `ValueError` / `TypeError` (no custom subclass) — consistent with `prediction_log.py` which also uses plain `ValueError` for input validation (custom subclasses are reserved for errors with API-mapped codes).

**Verdict:** Error handling follows the established pattern.

---

### 11. PASS — Logging style

- `callback_metrics.py` and `settlement.py` and `feature_snapshot.py` use **no logging** (pure stores) — consistent with `prediction_log.py` which also has no logging.
- `worker.py` (settlements) uses **no logging** — consistent with a pure worker module that delegates to stores.
- `main.py` uses structlog via `get_logger(__name__)` with key-value event format: `log.warning("feature_health_write_failed", agent_id=..., symbol=..., error=str(exc))` — this is the correct structlog format (event name as first positional arg, key-value context as kwargs).
- `log.info("gbm.pred", symbol=..., direction=..., confidence=...)` — same structlog convention.
- The `feature_health_write_failed` log event is in the correct format: event name + structured kwargs. It uses `log.warning` (not `log.error`) which is appropriate for a best-effort sidecar failure that doesn't block the publish loop.

**Verdict:** Logging is consistent with the project's structlog conventions.

---

### 12. PASS — Dead code / unused imports

- `ruff check` (includes F401 unused-import detection) passes on all 8 target paths — no unused imports.
- The `# noqa: F401` on `sign_callback` in `gateway.py:68` is intentional (documented in the comment on lines 72-74: the import exists so the callback-security test can monkey-patch it).
- No leftover `print()` debug statements in production code (the `print()` calls in `train.py:474,500` are CLI output, not debug prints).
- No dead code found. The `_compat_sign_callback` function was removed (commit `65be033`) and its removal is clean — no dangling references.
- `train.py:215`: `_ = embargo_bars  # silence "unused arg" lint` — this is a documented intentional no-op to preserve the API contract. Not dead code.

**Verdict:** Clean — no dead code or unused imports.

---

### 13. PASS — Docstring style

All new modules follow the baseline `prediction_log.py` docstring structure:
- **Module-level:** "Why this module exists" / "Filesystem layout" / "Design constraints" sections with `~~~` underlines.
- **Class-level:** Every class has a docstring explaining its purpose, invariants, and field semantics.
- **Function-level:** Every public function has a docstring with Args/Returns/Raises semantics where applicable.
- **Section separators:** `# ---...--- #` style used consistently in `settlement.py`, `feature_snapshot.py`, `callback_metrics.py`, `worker.py` — matches `prediction_log.py`.

**Verdict:** Docstrings are comprehensive and follow the project convention.

---

### 14. PASS — Naming conventions

- **snake_case** for all functions, methods, variables, modules.
- **PascalCase** for all classes (`ApprovedRoots`, `SettlementStore`, `FeatureSnapshotStore`, `CallbackMetricsStore`, `FeatureHealthRow`, `LogRegBaseline`).
- **UPPER_SNAKE_CASE** for constants (`DEFAULT_COST_MODEL_VERSION`, `_FEE_BPS`, `_BAD_NAME_CHARS`, `SETTLEMENT_NOW_NS`).
- **_leading_underscore** for internal helpers (`_validate_agent_id`, `_encode_line`, `_decode_line`, `_find_root`).
- **Literal** type alias for status enums (`SettlementStatus = Literal[...]`) — matches the project's use of `StrEnum` where appropriate.

**Verdict:** Naming conventions are fully consistent.

---

### 15. PASS — Type annotation completeness

- All public functions have complete type annotations (parameters and return types).
- Internal helpers are annotated.
- `from __future__ import annotations` is used in all new modules (enabling PEP 604 `X | None` syntax).
- The `worker.py` `Callable[[str, int, int], Awaitable[float | None]]` annotation for the market-data source is precise and correct.
- The only gap is the mypy `no-any-return` issues in `logreg.py` and `paper_spine_replay.py` (finding #2), which are strictness issues, not missing annotations.

**Verdict:** Type annotations are thorough and consistent with the project's strict-mypy stance.

---

## Verdict

**PASS with minor findings.**

The `ml-dataset-evidence-spine` implementation is a high-quality, convention-disciplined body of work. The new `fincept_core.datasets` package is a textbook example of matching an existing codebase's style — docstrings, section separators, error patterns, store shapes, and tolerance policies all mirror the `prediction_log.py` baseline precisely.

**Actionable items (ordered by priority):**

1. **(CONSISTENCY)** Fix `models.py:post_train` to let `ApprovedRootsError` propagate to the shared handler instead of catching inline — this also resolves the mypy `return-value` error. (Finding #1, #2)
2. **(STYLE)** Fix the 5 `no-any-return` mypy errors in `logreg.py` and `paper_spine_replay.py` with explicit casts or `np.asarray(..., dtype=float)` wrapping, so the new code passes mypy if added to CI. (Finding #2)
3. **(MINOR)** Add `from __future__ import annotations` to `baselines/__init__.py`. (Finding #5)
4. **(MINOR)** Consider replacing `WalkForwardWindow.to_dict()` with `model_dump()` for Pydantic v2 consistency. (Finding #4)

**Non-actionable (documented design decisions):**
- `_validate_agent_id` duplication across 3 stores — intentional, documented. (Finding #3)
- `LogRegBaseline` mutable dataclass — intentional for a model object. (Finding #7)
- Line-length violations — E501 not enforced by project config. (Finding #6)
- Import grouping of `settlements.worker` — ruff isort not enforced. (Finding #8)
