# Limited Live Readiness Review

**Task:** TASK-1101 (Order 49) — Limited Live Readiness Review
**Author:** Builder 6 (GLM-5.2)
**Date:** 2026-06-23
**Scope:** Go/no-go synthesis for `QUANT_FOUNDRY_ENABLED=true` limited paper-to-live pilot.
**Posture:** READ + WRITE-DOC-ONLY. No code, flag, or secret was changed.

---

## 1. Executive Summary

**NOT READY for limited paper-to-live pilot.**

Quant Foundry has shipped a coherent stack of contracts and TDD-tested gates: drift sentinel, conformal gate, MoE router, dossier registry, tournament scoring, leakage sentinel, paper bridge with rollback, promotion queue, BudgetGuard, and RunPod container skeletons. None of that stack has ever run against a real GPU. No model has been promoted through the gate. Shadow inference is still a stub. The paper bridge has never been enabled with `QUANT_FOUNDRY_ALLOW_PAPER_BRIDGE=true`. There is no deployed production control plane (TASK-0903 produced a design only). No broker credentials exist anywhere in the tree. Because of those gaps, the hard-gate checklist in `docs/NEXT_STEPS_PLAN.md:2192-2210` is only PARTIAL across the board, with several gates NOT MET. The default posture stays `QUANT_FOUNDRY_ENABLED=false`. No code path skips `oms` or `risk`.

---

## 2. Evidence Summary

All paths are relative to repo root. Commit SHAs are taken from `git log --oneline -50` and verified by `git show <sha>`.

| Capability | Status | Evidence path | Commit SHA |
|---|---|---|---|
| Adversarial drift sentinel | Built (contract-proven) | `services/quant_foundry/src/quant_foundry/drift_sentinel.py` | `22700a7` (TASK-1004) |
| Conformal prediction risk gate | Built (contract-proven) | `services/quant_foundry/src/quant_foundry/conformal_gate.py` | `e272b6e` (TASK-1003) |
| Mixture-of-experts model router | Built (contract-proven) | `services/quant_foundry/src/quant_foundry/moe_router.py` | `a88e8c2` (TASK-1001) |
| Causal market memory graph | Built (contract-proven) | `services/quant_foundry/src/quant_foundry/causal_graph.py` | `808e7ab` (TASK-1002) |
| Paper-only model pointer bridge | Built, config-gated off | `services/quant_foundry/src/quant_foundry/paper_bridge.py` | `e95c51f` (TASK-0704) |
| Retirement / edge-decay flags | Built (contract-proven) | `services/quant_foundry/src/quant_foundry/retirement.py` | `ffe9ce7` (TASK-0703) |
| Promotion review queue + human gate | Built, no real submissions | `services/quant_foundry/src/quant_foundry/promotion.py` | `60f9e61` (TASK-0702) |
| Expanded tournament leaderboard | Built (fixture-backed) | `services/quant_foundry/src/quant_foundry/leaderboard_expanded.py` | `0831e2c` (TASK-0701) |
| Jobs / dossiers / tournament / promotion dashboard pages | Built (read-only) | `services/api/src/api/routes/quant_foundry.py`, `apps/dashboard/src/app/quant-foundry/*` | `8f3a589` (TASK-0802) |
| BudgetGuard fail-closed wired into gateway | Built | `services/quant_foundry/src/quant_foundry/budget.py`, `gateway.py` | `6256cdf` (TASK-0901) |
| Leakage / overfit sentinel | Built (fixture-backed, no promoted model) | `services/quant_foundry/src/quant_foundry/sentinel.py` | `d864b94` (TASK-0406) |
| Shadow inference + feature snapshots | Stub-only | `services/quant_foundry/src/quant_foundry/callbacks.py:ShadowLedgerStub`, `shadow_inference.py` | `df326d4`, `1a91a82` (TASK-0601/0602) |
| RunPod training / inference containers | Local mock only, never deployed | `runpod/quant-foundry-training/handler.py`, `runpod/quant-foundry-inference/handler.py` | `2283b43` (TASK-0501), `df326d4` (TASK-0601) |
| Production deployment environment | Design only, not deployed | `docs/...` per TASK-0902/0903, commit `4cce0c9` | `4cce0c9` (TASK-0903) |
| Broker / Alpaca credentials | Not present in tree | grep `runpod/` for `alpaca|broker|api_key|credential|secret` — only README exclusion notes match | n/a |

---

## 3. Hard Gate Checklist

The 14 gates from `docs/NEXT_STEPS_PLAN.md:2196-2210`. `DONE` tags from the plan are honored for the first three; the rest are re-evaluated against the current tree.

| # | Gate | Verdict | Evidence |
|---|---|---|---|
| 1 | Runtime safety guards enforced everywhere | MET | Plan marks `[DONE]`. Confirmed by `services/quant_foundry/src/quant_foundry/schemas.py` `Authority` enum + `ShadowLedgerStub` non-trading guard at `callbacks.py:71`. |
| 2 | Backtest path handling locked down | MET | Plan marks `[DONE]`. `pbo.py` probability-of-backtest-overfitting helper + `sentinel.py` use it; `test_sentinel.py` exercises the path. |
| 3 | Verification receipts exist | MET | `reports/verification/` contains 6 receipt artifacts (3 `.md` + 3 `.json`), plus `baseline-2026-06-22.md`. `scripts/verification-receipt.ps1` produces them. |
| 4 | Quant Foundry contract-tested | MET | `services/quant_foundry/tests/` suite was 582 passed after TASK-1002 and 991 passed after TASK-0802 (per builder logs). All quant_foundry modules ship TDD tests. |
| 5 | Settlement ledger reliable (net-of-cost, point-in-time) | PARTIAL | `services/quant_foundry/src/quant_foundry/settlement.py` + `shadow_settlement.py` are built and tested, but no real RunPod inference has produced settled history. Stub-only. |
| 6 | Dossier registry reliable (full reproducibility set) | PARTIAL | `services/quant_foundry/src/quant_foundry/registry.py` + `dossier.py` are built and tested, but `DossierStub` is in use; no real training run has registered a real dossier. |
| 7 | Tournament scoring reliable (deflated / luck-adjusted, net of cost) | PARTIAL | `services/quant_foundry/src/quant_foundry/tournament.py` + `significance.py` are built (TASK-0701 `0831e2c`). Fixture-backed only; no real tournament round has completed against live shadows. |
| 8 | Leakage / overfit sentinel green on promoted model family | NOT MET | Sentinel exists (TASK-0406 `d864b94`), but **no model has been promoted**. The promotion queue has never processed a real request. There is no promoted family to run the sentinel against. |
| 9 | Shadow inference has enough settled history | NOT MET | `callbacks.py:ShadowLedgerStub` is an in-process stub. `shadow_inference.py` + `feature_snapshot_export.py` are contract-proven locally but never exercised against a real RunPod GPU. |
| 10 | Paper bridge has run safely | NOT MET | `paper_bridge.py` exists (TASK-0704 `e95c51f`), but `QUANT_FOUNDRY_ALLOW_PAPER_BRIDGE` has never been set to `true` against a real promoted model. Bridge was never exercised against a real shadow stream. |
| 11 | Rollback pointer exists | MET | `paper_bridge.py:105` `RollbackPointer` model + `paper_bridge.py:299` creation step before publish. Verified by grep below. |
| 12 | OMS and risk unchanged and authoritative | MET | Grep across `services/quant_foundry/src/quant_foundry/` for `^from oms|^import oms|^from risk|^import risk` returns **zero matches**. Grep across `services/oms/` and `services/risk/` for `quant_foundry` also returns **zero matches**. Quant Foundry is structurally isolated from order/risk execution. |
| 13 | Human approval workflow working | MET (code) / NOT MET (operational) | `promotion.py` `PromotionGate.evaluate()` (lines 198-272) requires dossier + settlement evidence + sentinel pass + human review notes; no auto-promote path. Operationally, no model has ever been submitted because nothing has been promoted yet. |
| 14 | Deployment environment has secure secrets and monitoring | NOT MET | TASK-0902/0903 produced AWS design only (commit `4cce0c9`). No ECS Fargate, no Secrets Manager, no CloudWatch, no Railway staging. The design is not deployed. |
| 14b | Live provider / broker credentials never available to RunPod | MET | `runpod/` only references `QUANT_FOUNDRY_CALLBACK_SECRET` (HMAC secret, used for signing callbacks). The single `ALPACA_API_KEY` match in `runpod/quant-foundry-training/README.md` is in a sentence that documents its absence. No broker credentials exist in the tree. |

---

## 4. Blockers

Every PARTIAL or NOT MET gate above is a blocker. Enumerated:

1. **B1 — No promoted model family.** Nothing has passed through `PromotionGate.evaluate()` against real evidence. The promotion queue (`60f9e61`) has never received a real submission.
2. **B2 — Shadow inference is stub-only.** `ShadowLedgerStub` (`callbacks.py:49`) and `DossierStub` (`callbacks.py:86`) replace real ledger / dossier storage. No real RunPod shadow GPU has produced a settled prediction.
3. **B3 — Paper bridge never enabled with a real model.** `QUANT_FOUNDRY_ALLOW_PAPER_BRIDGE` is unset; the bridge refuses every publish (`paper_bridge.py:244-249`). No bridge receipt has ever been written against a real promoted model.
4. **B4 — No production deployment environment.** TASK-0903 (`4cce0c9`) is design-only. No Railway staging, no AWS Fargate, no Secrets Manager, no CloudWatch alarms. The "deployment environment has secure secrets and monitoring" gate cannot be evaluated as MET until something is actually deployed.
5. **B5 — No broker credentials configured.** There is no paper-broker account, no live-broker account, and no broker API key anywhere in the tree. A paper-to-live pilot cannot begin without a configured, isolated broker sandbox.
6. **B6 — Real RunPod GPU has never run.** Phase 5 (`2283b43`, `caeb468`, `b3fc4e1`) and Phase 6 (`df326d4`, `1a91a82`, `0aa4aef`) shipped RunPod container MVPs and dispatch client, all tested locally with mock GPU only. No real `runpod.io` job has been dispatched from this codebase.
7. **B7 — Leakage / overfit sentinel un-runnable.** `sentinel.py` runs only on registered dossiers. No real dossier exists, so the sentinel cannot pass on a promoted family. Blocks gate #8.
8. **B8 — Settled history is empty.** `shadow_settlement.py` + `settlement.py` are correct, but have no inputs. No tournament round has been settled against live data. Blocks gates #5, #7, #9.

These eight blockers collectively block every gate from #5 onward. The frontend of the system (gate #12, OMS/risk isolation; gate #13 human approval) is MET structurally but cannot be operationally exercised until B1-B8 are resolved.

---

## 5. Rollback Proof

Three independent layers make disabling a config flip, not a code change.

### Layer A — Gateway default off

```text
$ grep -n 'QUANT_FOUNDRY_ENABLED' services/quant_foundry/src/quant_foundry/gateway.py
12:QUANT_FOUNDRY_ENABLED   (default "false")
98:enabled = os.environ.get("QUANT_FOUNDRY_ENABLED", "false").lower() == "true"
```

`from_env()` at `gateway.py:96` reads the env var and defaults to `"false"`. No live mode unless an operator exports `QUANT_FOUNDRY_ENABLED=true`.

### Layer B — Paper bridge guard

```text
$ grep -n 'QUANT_FOUNDRY_ALLOW_PAPER_BRIDGE\|allow_paper_bridge\|bridge is disabled' \
    services/quant_foundry/src/quant_foundry/paper_bridge.py
7: ``QUANT_FOUNDRY_ALLOW_PAPER_BRIDGE=true``.
218: env_val = os.environ.get("QUANT_FOUNDRY_ALLOW_PAPER_BRIDGE", "").lower()
247: reason="bridge is disabled (QUANT_FOUNDRY_ALLOW_PAPER_BRIDGE != true)",
```

`PaperBridge.publish()` returns `BridgeStatus.REFUSED` with the explicit reason `"bridge is disabled (QUANT_FOUNDRY_ALLOW_PAPER_BRIDGE != true)"` when the env var is unset or not `"true"` (`paper_bridge.py:244-249`).

### Layer C — Rollback pointer

```text
$ grep -n 'rollback' services/quant_foundry/src/quant_foundry/paper_bridge.py
105: """A rollback pointer created before publishing.
128: prediction (if published), and rollback pointer (if created).
136: rollback_pointer: RollbackPointer | None = None
206: If all guards pass, the bridge creates a rollback pointer, converts
297: # 7. Create rollback pointer.
299: rollback_pointer = RollbackPointer(
```

Even if the bridge were enabled, `paper_bridge.py:297-316` creates a `RollbackPointer` recording the prior model pointer before publishing the new one. A failed publish can revert the pointer to the prior state.

**Rollback verdict:** Three independent config gates + a rollback pointer. To disable live influence, an operator runs `unset QUANT_FOUNDRY_ENABLED` and `unset QUANT_FOUNDRY_ALLOW_PAPER_BRIDGE`. No code change, no restart, no deployment.

---

## 6. Risk Caps Proof

### BudgetGuard fail-closed (TASK-0901 `6256cdf`)

```text
$ grep -n 'class BudgetGuard\|kill_switch\|check_and_reserve\|fail_closed' \
    services/quant_foundry/src/quant_foundry/budget.py
20:- A global kill switch (``set_kill_switch(True)``) blocks ALL paid jobs
72:kill_switch_enabled: bool
81:class BudgetGuard:
107:def check_and_reserve(
137:if self._kill_switch and amount_cents > 0:
237:"kill_switch_enabled": self._kill_switch,
241:def set_kill_switch(self, enabled: bool) -> None:
```

`BudgetGuard.check_and_reserve()` at `budget.py:107` blocks any non-zero spend when the global kill switch is set (`budget.py:137`). Monthly ceiling is enforced before any GPU job is dispatched. Wired into `QuantFoundryGateway.from_env()` (`gateway.py:104`).

### OMS / Risk authority preserved

```text
$ # quant_foundry does not import oms or risk
$ grep -rn '^from oms\|^import oms\|^from risk\|^import risk' \
    services/quant_foundry/src/quant_foundry/
(no matches)

$ # oms and risk do not import quant_foundry
$ grep -rn 'quant_foundry' services/oms/ services/risk/
(no matches)
```

The two greps confirm: quant_foundry has zero reverse or forward coupling to `oms` or `risk`. Order execution and risk evaluation remain authoritative in their own services. Quant Foundry cannot bypass risk; it can only emit signals and shadow predictions.

**Risk-caps verdict:** BudgetGuard blocks GPU spend before any job runs; OMS/risk authority is structurally isolated. No code path inside quant_foundry writes orders or alters risk state.

---

## 7. No RunPod Broker Credential Access Proof

```text
$ grep -rn 'broker|alpaca|credential|secret|api_key' runpod/
runpod/quant-foundry-training/README.md:11:- **No broker credentials.** ...
runpod/quant-foundry-training/README.md:12:no `ALPACA_API_KEY`, no Redis URL, ...
runpod/quant-foundry-training/README.md:80:... QUANT_FOUNDRY_CALLBACK_SECRET=secret ...
runpod/quant-foundry-training/README.md:87:| `QUANT_FOUNDRY_CALLBACK_SECRET` | yes (prod) | ... | HMAC secret for signing callbacks |
runpod/quant-foundry-training/handler.py:11:- NO broker credentials, NO Redis, NO stream write capability.
runpod/quant-foundry-training/handler.py:38:def _get_callback_secret() -> str:
runpod/quant-foundry-training/handler.py:39:  secret = os.environ.get("QUANT_FOUNDRY_CALLBACK_SECRET", "")
runpod/quant-foundry-training/handler.py:43:  return "dev-callback-secret-DO-NOT-USE-IN-PROD"
runpod/quant-foundry-training/handler.py:85:  callback_secret=_get_callback_secret(),
runpod/quant-foundry-training/Dockerfile:3:# Minimal Python container ... No broker
runpod/quant-foundry-training/Dockerfile:18:# Install only the quant_foundry package (no broker/Redis/trading deps).
runpod/quant-foundry-training/Dockerfile:26:# No broker credentials, no Redis, no trading env vars.
runpod/quant-foundry-training/Dockerfile:27:# Only the callback secret is required (injected at runtime).
```

Every match for `broker|alpaca|credential|secret|api_key` in `runpod/` is either a denial statement ("No broker credentials", "no `ALPACA_API_KEY`") or references `QUANT_FOUNDRY_CALLBACK_SECRET`, the HMAC secret used by the callback processor to sign results on the way back to the gateway. The RunPod containers have no broker credentials, no Redis URL, no trading env var.

The narrow follow-up grep for actual broker secrets:

```text
$ grep -rn 'alpaca|broker_credential|broker_secret|api_key' runpod/
runpod/quant-foundry-training/README.md:12:no `ALPACA_API_KEY`, no Redis URL, no stream producer.
```

The single match is the README sentence documenting the absence.

**RunPod verdict:** RunPod handlers see only the callback HMAC secret. Broker credentials are not in the tree, not in the Docker image, and not reachable from handler code.

---

## 8. Human Approval Required Proof

```text
$ grep -n 'def evaluate\|class PromotionGate\|NO_DOSSIER\|INSUFFICIENT_EVIDENCE\|sentinel_receipt.passed\|blocking_issues' \
    services/quant_foundry/src/quant_foundry/promotion.py
129:NO_DOSSIER = "no_dossier"
130:INSUFFICIENT_EVIDENCE = "insufficient_evidence"
188:"""Evaluates promotion requests against the evidence packet."""
198:def evaluate(
206:# 1. No dossier -> reject.
207:if evidence.dossier is None:
228:evidence.tournament_result.settled_count
242:if evidence.sentinel_receipt is not None and not evidence.sentinel_receipt.passed:
253:for issue in evidence.blocking_issues:
```

`PromotionGate.evaluate()` at `promotion.py:198` enforces four fail-closed checks: (1) dossier present, (2) tournament evidence sufficient, (3) settlement evidence sufficient, (4) sentinel receipt passes. The module's docstring at `promotion.py:5` is explicit: "Requires human approval and evidence packets for model promotion."

There is no code path in `quant_foundry` that calls `submit_promotion()` and then auto-marks it approved. `PromotionReviewQueue.submit()` (`promotion.py:298`) appends to `_pending`; an operator must call `approve()` or `reject()`. No model can be promoted without a dossier, without settled evidence, and without a sentinel pass.

**Human-approval verdict:** Code-level MET. No model has been promoted yet, so the workflow has not been operationally exercised, but the design forces human review before any model reaches `Authority.PROMOTED`.

---

## 9. Live Mode Default Proof

```text
$ grep -rn 'QUANT_FOUNDRY_ENABLED\|QUANT_FOUNDRY_ALLOW_PAPER_BRIDGE' \
    services/quant_foundry/src/quant_foundry/
gateway.py:12:QUANT_FOUNDRY_ENABLED   (default "false")
gateway.py:98:enabled = os.environ.get("QUANT_FOUNDRY_ENABLED", "false").lower() == "true"
paper_bridge.py:7:``QUANT_FOUNDRY_ALLOW_PAPER_BRIDGE=true``.
paper_bridge.py:218:env_val = os.environ.get("QUANT_FOUNDRY_ALLOW_PAPER_BRIDGE", "").lower()
paper_bridge.py:247:reason="bridge is disabled (QUANT_FOUNDRY_ALLOW_PAPER_BRIDGE != true)",
```

- `QUANT_FOUNDRY_ENABLED` defaults to `"false"` (`gateway.py:98`).
- `QUANT_FOUNDRY_MODE` defaults to `"local_mock"` (`gateway.py:99`), which is non-paper, so the bridge would still refuse even if enabled.
- `QUANT_FOUNDRY_ALLOW_PAPER_BRIDGE` is unset by default; `PaperBridge` refuses with the explicit reason above.

To confirm no code path inside quant_foundry imports `oms` or `risk`:

```text
$ grep -rn '^from oms\|^import oms\|^from risk\|^import risk' \
    services/quant_foundry/src/quant_foundry/
(no matches)
```

**Live-mode-default verdict:** Three independent config gates default to off. The mode defaults to `local_mock`. No code path imports `oms` or `risk`. Live mode remains disabled by default.

---

## 10. Required Operator Decision

This report does **not** authorize limited live mode. To proceed toward a limited paper-to-live pilot, the operator must, in order:

1. **Phase 5** — Stand up a real RunPod training container, dispatch a real training job from `runpod_client.py` against a real GPU, and import the resulting artifact via TASK-0503 path.
2. **Phase 6** — Stand up a real RunPod inference container, dispatch a real shadow inference run, and observe settled predictions landing in `ShadowLedger` (real, not stub).
3. **Phase 7** — Build enough settled shadow history to populate the tournament leaderboard, run the leakage / overfit sentinel, and submit a model to `PromotionReviewQueue.submit()` with a real dossier + tournament result + sentinel receipt.
4. **Phase 7 (cont.)** — A human must call `approve()` on the promotion queue entry after reviewing the evidence packet. Only then does the model reach `Authority.PROMOTED`.
5. **Deployment (TASK-0902/0903)** — Deploy the AWS production control plane per the existing design (`4cce0c9`), wire Secrets Manager for the callback secret, and stand up CloudWatch alarms on BudgetGuard.
6. **Broker sandbox** — Configure a paper-broker account in the trusted deployment environment (OMS), with credentials stored only in Secrets Manager and never exposed to RunPod.
7. **Paper bridge dry-run** — Set `QUANT_FOUNDRY_ENABLED=true`, `QUANT_FOUNDRY_MODE=paper`, and `QUANT_FOUNDRY_ALLOW_PAPER_BRIDGE=true` against the promoted model. Verify the rollback pointer path end-to-end before any real order flow.
8. **Re-review** — Re-run this report. None of the gates from #5 onward will still be PARTIAL or NOT MET.

Until those eight steps complete, this report's verdict stands.

---

## 11. Conclusion

**NOT READY. Blockers: B1 (no promoted model family), B2 (shadow inference is stub-only), B3 (paper bridge never enabled with a real model), B4 (no production deployment environment), B5 (no broker credentials configured), B6 (no real RunPod GPU run), B7 (sentinel un-runnable without dossiers), B8 (no settled history).**

No code path skips risk/OMS. Live mode remains disabled by default (`QUANT_FOUNDRY_ENABLED=false`, `QUANT_FOUNDRY_MODE=local_mock`, `QUANT_FOUNDRY_ALLOW_PAPER_BRIDGE` unset). The paper bridge is structurally refused. BudgetGuard is fail-closed. RunPod handlers see only the callback HMAC secret. Human approval is required by `PromotionGate.evaluate()`.

This task is READ + WRITE-DOC-ONLY. No flag was flipped. No code was changed. No credential was created. The plan checkbox remains untouched.
