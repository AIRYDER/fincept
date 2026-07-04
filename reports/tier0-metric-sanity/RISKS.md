# Risks

## 1. Threshold false positives (LOW)

Conservative thresholds could flag a genuinely exceptional (but real)
strategy as "implausible". Mitigation: thresholds are env-configurable
(`QF_METRIC_SANITY_*`), so an operator who has verified a metric is
real can raise the threshold without a code change. The raw value is
always preserved for review.

## 2. Callback payload shape change (LOW-MEDIUM)

`metrics_summary` now carries an additional `metric_sanity` key. Any
trusted-side consumer that asserts an **exact** key set on
`metrics_summary` (rather than checking for specific keys) could break.
Mitigation: the existing `_CALLBACK_REQUIRED_FIELDS` check only
requires `metrics_summary` to be present (not a specific key set), and
all existing tests pass. The signature covers the full payload, so
tamper-evidence is preserved.

## 3. Promotion floor interaction with mode rules (LOW)

The sanity check forces `promotion_eligible=False` as a hard floor
**after** the mode-aware logic runs. This means even a production run
with all gates passing is blocked if a critical metric is implausible.
This is intentional (fail-closed) and matches the roadmap's intent.
It can never *enable* promotion that the mode rules would have denied
(canary/research stay False).

## 4. Metric key alias coverage (LOW)

The sanity check only inspects known metric key aliases
(`sharpe_ratio`/`sharpe`/`deflated_sharpe`, etc.). A model family that
emits an unrecognized key name (e.g. `sharpe_ratio_annualized`) would
not be checked. Mitigation: aliases are centralized in
`_METRIC_ALIASES` and easy to extend; the canonical names match the
`real_trainer.py` output keys.

## 5. Non-numeric metric values (NEGLIGIBLE)

Non-numeric metric values are silently ignored (no crash). This is
correct behaviour — a non-numeric Sharpe is a different class of bug
that the sanity bounds are not designed to catch.

## 6. No handler.py behavioural change (NEGLIGIBLE)

The handler.py edit is a comment-only annotation. The actual
validation is centralized in `build_callback()` to avoid conflicting
with Builder 3's concurrent handler.py edits (artifact durability).
This means any code path that calls `build_callback()` automatically
gets the sanity check — a stronger guarantee than handler-only wiring.

## Next recommended task

Wire the trusted-side (dispatcher/gateway) to read
`metrics_summary["metric_sanity"]` and reject/log callbacks where
`status == "implausible"` even if the signature verifies — defense in
depth. This is a Tier 1 callback-ingestion concern.
