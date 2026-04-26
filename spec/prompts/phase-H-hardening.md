# Phase H · Hardening — Agent Prompts

**Tasks:** TASK-070 (chaos suite), TASK-071 (DB replication), TASK-072 (HSM keys), TASK-073 (mTLS mesh), TASK-074 (audit archival), TASK-075 (live venue adapter), TASK-076 (gradual rollout)
**Checkpoint:** SOC-2-equivalent internal audit passes; DR drill completes within RTO; first $1k live capital allocation monitored 24×7 for 7 days without incident.

This phase is the gate to live capital. Until the entire phase passes, TRADING_MODE=paper.

---

## Phase kickoff

```text
You are now hardening a working paper-trading system into a live-trading system. The work in this phase looks unglamorous compared to Phase X, but it is what separates platforms that quietly lose money in production from those that survive their first real market drawdown.

PHASE-SPECIFIC RULES:

1. ASSUME EVERYTHING WILL FAIL. The exchange API will rate-limit. The DB will crash mid-write. A model will produce NaN. The kill switch will fail to activate exactly once when you most need it. Engineer for these. Test them. Don't hope.

2. NO LIVE TRADING UNTIL ALL TASKS DONE. Even if a single task is "almost done", TRADING_MODE stays paper. The temptation to flip live "for a small test" is how firms blow up. Resist.

3. AUDIT TRAIL IS LEGAL EVIDENCE. From Phase H onward, audit_log is treated as production-grade financial records. 7-year retention. WORM (write-once-read-many) storage. Immutable. Tamper-evident hash chain.

4. SECRETS IN HSM, NOT FILES. By end of Phase H, no API key sits in a .env file in production. They live in HSM (or cloud KMS); applications fetch ephemeral credentials.

5. CHAOS TESTS RUN IN CI. Failure injection is part of the test suite, not a quarterly fire drill. Every PR exercises basic chaos (kill a service, partition the network, fill a disk).

6. STAGED ROLLOUT IS NON-NEGOTIABLE. Live capital starts small ($1k for week 1, $5k week 2, growing only on clean weeks). Anyone who proposes "let's just deploy at full size, the backtest looks good" should be politely escorted to a different role.

7. RISK COMMITTEE OVERSIGHT. Daily P&L review during the rollout. Weekly stop-or-go decision. The committee can halt the rollout at any moment with no debate.

CONTEXT TO LOAD:
- spec/CONTRACTS.md (no schema changes — but you'll add audit fields).
- docs/RISKS.md (the risks Phase H mitigates).
- All TASK-04x specs (the singletons whose state you replicate / chaos-test).
- Compliance counsel (external).

WHEN STUCK:
- "We don't have time for this." That phrase indicates the rollout is going wrong. Slow down.
- Live venue API behavior surprises you? It will. Your reconnect / rate-limit / partial-fill code is now under stress, not your alpha. Fix the connection layer first; alpha decisions are downstream of working plumbing.

Acknowledge by listing the 7 rules. Wait for the first task.
```

---

## TASK-070 prompt — Chaos engineering suite

```text
Implement TASK-070 — automated failure injection.

Files:
- tests/chaos/__init__.py — chaos test infrastructure (pytest plugin).
- tests/chaos/scenarios/network_partition.py — partition Redis and Postgres.
- tests/chaos/scenarios/service_kill.py — SIGKILL random services.
- tests/chaos/scenarios/disk_full.py — fill /var.
- tests/chaos/scenarios/cpu_starve.py — pin a process to 100% CPU.
- tests/chaos/scenarios/clock_skew.py — drift system clock.
- tests/chaos/scenarios/exchange_outage.py — sever WebSocket from venue.
- .github/workflows/chaos.yml — runs nightly on a dedicated runner.

Tooling:
- toxiproxy for network partition + latency injection.
- A simple Python harness that runs each scenario against a docker-compose'd stack.

Per scenario test:
1. Start full stack (ingestor, agents, orchestrator, risk, oms, portfolio, api).
2. Generate steady-state traffic (synthetic Decisions every second).
3. Inject the failure.
4. Verify:
   - The system as a whole does NOT lose data already accepted.
   - Affected service emits clear alerts within 30s.
   - Once failure resolves, system reaches steady state within 60s.
   - No duplicate orders / fills.

Author spec/tasks/TASK-070-chaos.md, implement.

Acceptance:
- All 6 scenarios green in CI.
- Run nightly. Failures page on-call.
```

---

## TASK-071 prompt — Postgres replication + DR drill

```text
Implement TASK-071 — Postgres physical replication and documented failover.

Files:
- infra/k8s/postgres.yaml — primary + standby StatefulSets.
- infra/postgres/postgresql.conf — replication config.
- infra/postgres/recovery.conf — standby config.
- docs/runbooks/db-failover.md — operator runbook.
- tests/dr/test_db_failover.py — automated drill.

Setup:
- Streaming replication, hot_standby=on, wal_level=replica.
- 1 primary + 1 sync standby + 1 async standby (different AZ).
- WAL archiving to S3-compatible (MinIO in dev, AWS S3 in prod).

Failover procedure:
1. Detect primary failure (timeouts, health-check fail).
2. Promote sync standby to primary.
3. Reroute write traffic via PgBouncer + connection-string update (Redis-published).
4. Reattach old primary as new standby once it recovers.

Specific landmines:
- Synchronous replication adds latency; sync standby commits before primary acks. Use it only if RPO=0 is mandatory; otherwise async with documented data-loss-window suffices.
- Failover must be FAST (RTO < 30s) but not TOO fast (auto-failover during transient network blip causes split-brain). Use Patroni or pg_auto_failover.

Author spec/tasks/TASK-071-pg-replication.md, implement.

Acceptance:
- Quarterly DR drill: primary killed, standby promoted, system resumes within RTO. Tested by an engineer not on the DB team.
```

---

## TASK-072 prompt — HSM-backed exchange API keys

```text
Implement TASK-072 — exchange API keys in HSM (or cloud KMS), zero withdrawal scope.

Files:
- libs/fincept-core/src/fincept_core/secrets.py — abstraction over Vault / AWS KMS / HashiCorp HSM.
- infra/secrets/policies.hcl — Vault policies (least privilege).
- docs/runbooks/key-rotation.md — operator procedure.

Key handling rules:
- Exchange API keys: TRADE scope only. NEVER WITHDRAW. Document this requirement; verify on each exchange's API console screenshot stored in /docs/compliance/.
- IP allowlisting: each exchange API key restricted to known production IPs.
- Rotation: every 90 days. Automated.
- Local dev: use sandbox/testnet keys with no real funds.

App integration:
- At startup, services request key from secrets API; cache in memory only.
- On rotation event (Redis pub/sub), services refresh.

Author spec/tasks/TASK-072-hsm-keys.md, implement.

Acceptance:
- No grep across services finds a literal API key. Tests fail otherwise.
- Rotation drill: rotate Binance key, all services pick up within 60s, no missed market data.
```

---

## TASK-073 prompt — mTLS service mesh

```text
Implement TASK-073 — mTLS between all services via Istio (or Linkerd).

Files:
- infra/k8s/istio/ — Istio install + PeerAuthentication policies.
- infra/k8s/cert-manager/ — auto-cert provisioning.
- docs/runbooks/cert-renewal.md.

Policy:
- Strict mTLS for all in-cluster traffic.
- Service-to-service AuthorizationPolicies enforce least privilege (e.g., orchestrator can publish to ord.decisions but not ord.fills).
- Certificates auto-renewed via cert-manager; alerts if expiry < 7 days.

Specific landmines:
- mTLS adds latency (~1-2ms per hop). Verify still within SLOs under load.
- Local dev: provide a make target to skip Istio for fast iteration.

Author spec/tasks/TASK-073-mtls.md, implement.

Acceptance:
- Plain HTTP between services rejected (verified by chaos test).
- All certs auto-renewed; no manual intervention in 90-day window.
```

---

## TASK-074 prompt — Audit log archival (WORM, 7yr)

```text
Implement TASK-074 — long-term immutable audit storage.

Files:
- services/jobs/src/jobs/archive.py — daily archival job.
- infra/s3/bucket-policy.json — S3 Object Lock + bucket policy.

Behavior:
- Daily: dump the previous day's audit_log rows to compressed JSONL in S3.
- S3 bucket: Object Lock enabled, 7-year retention, governance mode (admin can extend, not shorten).
- Each daily archive includes a SHA-256 hash chained to previous day. Tamper detection.

Verification:
- Monthly job re-reads a random week's archive, verifies hash chain, restores into a sandbox DB, audits row counts vs source.

Author spec/tasks/TASK-074-archive.md, implement.

Acceptance:
- 90-day continuous archive runs without gap.
- Restore drill: pick a random day from 60 days ago; restore from archive into sandbox; row count matches.
```

---

## TASK-075 prompt — Live venue adapter (Binance first)

```text
Implement TASK-075 — the first live exchange adapter (Binance spot), gated behind multiple safety layers.

Files:
- services/oms/src/oms/venue/binance.py — BinanceVenueAdapter (live).
- services/oms/src/oms/venue/base.py — VenueAdapter ABC.

Safety layers (all must be true to even attempt a live order):
1. settings.trading_mode == "live"
2. Phase H all tasks marked [x] in BUILD_ORDER.md (programmatic check).
3. Risk committee approval flag in DB (set manually by COO).
4. Per-venue notional cap (default $1k week 1, escalating per TASK-076 schedule).
5. Withdrawal scope on the API key is OFF (verified at startup).
6. IP allowlist verified.

Functionality:
- submit_order: REST POST /api/v3/order with HMAC signing.
- cancel_order: DELETE /api/v3/order.
- on_fill: parse Binance executionReport from user-data WebSocket; emit Fill on ord.fills.
- reconnect on user-data stream every 23h (Binance keepalive limit).

Specific landmines:
- Time sync: Binance rejects orders with timestamp drift > 1s. NTP sync mandatory; check at startup.
- Rate limits: 1200 weight/minute. Implement weight-aware throttler.
- Float precision: Binance returns strings; convert via Decimal. Never round-trip through float.
- ListenKey rotation for user-data WS: separate task; don't conflate with main connection.
- Test in TESTNET first. Only after PASSING production-mirror integration tests against testnet, switch to live keys.

Author spec/tasks/TASK-075-live-binance.md, implement.

Acceptance:
- 100 successful round-trip orders against TESTNET (submit → fill → position update).
- Documented 0 incidents during a 7-day testnet shadow.
- Risk committee sign-off documented.
- All 6 safety-layer checks unit-tested.
```

---

## TASK-076 prompt — Gradual rollout harness

```text
Implement TASK-076 — staged rollout from paper to limited live.

Files:
- services/oms/src/oms/rollout.py — RolloutController.
- docs/runbooks/rollout-schedule.md — week-by-week plan.

Schedule (default; risk committee can adjust):
- Week 0: shadow only (live signals, paper OMS).
- Week 1: $1,000 max gross notional. Monitor 24×7. Halt on any anomaly.
- Week 2: $5,000 (only if week 1 P&L within ±2σ of paper expectation).
- Week 3: $25,000.
- Week 4+: scale per risk committee.

Halting triggers (any one fires automatic kill switch + page on-call):
- Daily loss > 1.5× MAX_DAILY_LOSS_USD.
- Position count > 2× expected.
- Order rejection rate > 5%.
- Any "kill_switch.activated" log within 4 hours of last clear.
- VaR breach.

Files:
- services/oms/src/oms/rollout.py — implements the gating + halting logic.
- services/jobs/src/jobs/rollout_review.py — daily summary email to risk committee.

Specific landmines:
- "Just kidding, restore prior limits" must be a manual operation requiring a 4-eye approval (two engineers + risk committee chair).
- Halting must be sticky — once halted, requires explicit unhalt; system does not auto-resume.

Author spec/tasks/TASK-076-rollout.md, implement.

Acceptance:
- Week 1 simulated rollout in chaos suite: synthetic anomaly fires every halting trigger; system halts within 30s; no orders submitted post-halt.
```

---

## Phase H exit verification (the live-capital gate)

```text
Run the Phase H checkpoint validation. This is the gate to real money. Be slow. Be skeptical.

1. Internal audit (SOC-2 equivalent):
   - Access controls: every prod resource requires MFA-backed identity. List of who has prod access reviewed.
   - Audit logs: random sample of 50 trading decisions reconstructed end-to-end. All present, all consistent.
   - Change management: 90 days of merge history reviewed. Every prod-affecting change has PR review + CI green.
   - Disaster recovery: drill executed in last 30 days. Met RTO/RPO.

2. Chaos suite green for 14 consecutive nightly runs.

3. Live key safety:
   - Withdrawal scope confirmed OFF on every exchange via screenshot in /docs/compliance/.
   - Key rotation drilled. Took < 60s end-to-end with zero data gap.

4. Rollout dry run:
   - Synthetic week 1: all halting triggers tested individually; each fires correctly.
   - 4-eye unhalt procedure rehearsed.

5. Risk committee briefing:
   - Document presented to committee: scope, capital, halting triggers, kill plan, DR plan.
   - Committee written approval to proceed with $1k week 1.
   - Daily P&L review schedule confirmed.

6. Operational on-call:
   - 24×7 rotation staffed and rehearsed.
   - Pager duty integration tested (synthetic alert fires, pages within 60s).
   - Kill-switch drill: on-call engineer woken at 3am, halts trading via UI in < 30s from page.

If all six pass, the system is GREEN-LIT for week 1 ($1,000 max). Mark tasks 070–076 as [x]. Add "Checkpoint H: passed YYYY-MM-DD". Set TRADING_MODE=live in production. Begin TASK-076 rollout schedule.

DO NOT skip steps. Every step here exists because a previous firm somewhere lost money to its absence. The hour you spend on the audit is the hour that prevents the loss.

After 30 days of clean live operation, retire this prompt. Operations becomes BAU + ongoing Phase X iteration. Welcome to the boring success state.
```
