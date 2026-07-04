# Remaining Ruff Debt

## Current state

- **Total errors:** 357 (down from 1845 — 80.6% reduction)
- **Auto-fixable:** 2
- **Unsafe-fixable:** 157 (need manual review)

## Top error categories

| Count | Code | Rule | Fix approach |
|-------|------|------|-------------|
| 60 | T201 | print statements | Replace with `logging.info()` or add `# noqa: T201` for CLI tools |
| 37 | F841 | unused variables | Remove the assignment or use `_` prefix |
| 31 | E402 | module import not at top | Move imports to top or add `# noqa: E402` for conditional imports |
| 26 | S110 | try-except-pass | Add logging or re-raise; some may be intentional (add `# noqa`) |
| 21 | SIM108 | if-else instead of if-expression | Convert to ternary or add `# noqa: SIM108` if readability matters |
| 19 | RUF001 | ambiguous unicode characters | Replace with ASCII equivalents (e.g., — → --) |
| 17 | SIM102 | collapsible-if | Merge nested if statements |
| 16 | SIM117 | multiple-with-statements | Merge nested with statements |
| 12 | SIM103 | needless-bool | Simplify boolean logic |
| 11 | S112 | try-except-continue | Add logging or re-raise |
| 10 | E741 | ambiguous variable name | Rename (l, I, O) |
| 10 | RUF002 | ambiguous unicode in docstring | Replace with ASCII |
| 8 | RUF005 | collection-literal-concatenation | Use `[*a, *b]` instead of `a + b` |
| 8 | S311 | suspicious non-crypto random | Use `secrets` module for security-sensitive code |
| 7 | RUF043 | pytest-raises-ambiguous-pattern | Use specific exception types in `pytest.raises()` |
| 7 | S108 | hardcoded temp file | Use `tempfile` module |
| 7 | S607 | start-process-with-partial-path | Use full path to executable |
| 6 | RUF046 | unnecessary cast to int | Remove `int()` call |
| 6 | S603 | subprocess without shell=true | Add `shell=True` or use `subprocess.run()` safely |
| 5 | RUF003 | ambiguous unicode in comment | Replace with ASCII |
| 5 | S310 | suspicious url open | Validate URL scheme before opening |
| 5 | SIM105 | suppressible-exception | Use `contextlib.suppress()` |
| 3 | E731 | lambda assignment | Convert to `def` function |
| 3 | RUF015 | unnecessary iterable allocation | Use `[0]` instead of `list(x)[0]` |
| 3 | S301 | suspicious pickle usage | Use `json` or add `# noqa: S301` for trusted data |
| 3 | UP042 | replace str-enum | Use `enum.StrEnum` (Python 3.11+) |
| 2 | B904 | raise without from | Add `from exc` to re-raises |
| 2 | UP015 | redundant open modes | Remove explicit `r` mode |
| 1 | ASYNC251 | blocking sleep in async | Use `asyncio.sleep()` |
| 1 | B007 | unused loop control variable | Rename to `_` or use it |
| 1 | F401 | unused import | Remove the import |
| 1 | RUF012 | mutable class default | Use `frozenset` or `tuple` |
| 1 | S104 | hardcoded bind all interfaces | Use `127.0.0.1` or config |
| 1 | S306 | suspicious mktemp usage | Use `mkstemp` instead |
| 1 | SIM118 | in-dict-keys | Use `key in dict` instead of `key in dict.keys()` |

## Recommended approach

1. **Do NOT run `ruff check --fix --unsafe-fixes`** — 157 unsafe fixes need manual review.
2. Start with the highest-count categories (T201, F841, E402) — these are mechanical fixes.
3. For S-rules (security), review each case individually — some may be intentional.
4. For SIM rules (simplicity), only fix if readability is not compromised.
5. For UP042 (StrEnum), this requires Python 3.11+ — safe to apply since the Dockerfile uses 3.12.
6. The UP017 fix (datetime.UTC) was reverted for Python 3.10 compat — re-apply once 3.10 support is dropped.
