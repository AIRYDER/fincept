# Fincept GitHub Hygiene Swarm Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Convert the current dirty Fincept worktree into clean, reviewable, GitHub-ready commits without merging unrelated generated files, secrets, or home/worktree noise.

**Architecture:** Use a coordinator plus narrow worker agents. Each worker owns one bounded surface, reports exact files touched, and stops instead of guessing when files cross surfaces. The coordinator is the only actor allowed to stage, commit, push, or open a PR.

**Tech Stack:** Git, GitHub CLI (`gh`), PowerShell on Windows, `uv run pytest`, Node/npm only for dashboard validation, Fincept monorepo paths under `C:\Users\nolan\CascadeProjects\fincept-terminal`.

---

## Current Known State

- Repo root: `C:\Users\nolan\CascadeProjects\fincept-terminal`
- Branch: `codex/portfolio-optimizer-core`
- Remote: `origin https://github.com/AIRYDER/fincept.git`
- Remote branch exists: `origin/codex/portfolio-optimizer-core`
- Upstream is not configured locally.
- `origin/main` is already contained in this branch; no merge or rebase from GitHub is needed before cleanup.
- GitHub has no open PR for `codex/portfolio-optimizer-core`.
- Dirty tree at last inspection:
  - 11 modified tracked files
  - 1199 untracked files

## Global Rules For All Worker Agents

- Do not run `git add .`, `git add -A`, `git clean`, `git reset --hard`, `git checkout --`, or force-push.
- Do not touch files outside the task's allowed file list.
- Do not delete untracked files. Classify them only.
- Do not commit secrets, local env files, local credentials, private keys, generated caches, `.worktrees/`, `.omo/`, `.opencode/`, `.playwright-cli/`, or runtime payload directories.
- If a file appears to belong to two task groups, stop and report it to the coordinator.
- Every worker must finish with:
  - files inspected
  - files modified
  - commands run
  - test result
  - remaining risk

## Swarm Topology

- **Coordinator:** Owns branch hygiene, staging, commits, pushes, and PR creation. Does not edit product files except `.gitignore` if a worker provides exact ignore lines.
- **Scout A: Git State:** Read-only branch/divergence checker.
- **Scout B: Secret And Artifact Risk:** Read-only risky-file classifier.
- **Worker C: Ignore Hygiene:** Updates ignore rules only after Scout B classifies generated/runtime paths.
- **Worker D: Quant Foundry Promotion Fix:** Owns the five files from the promotion-to-shadow-dispatch fix.
- **Worker E: Dashboard Design Tokens:** Owns dashboard token/UI files and their tests.
- **Worker F: Docs And Roadmap:** Owns roadmap, feature menu, release hygiene docs, and human-readable reports.
- **Worker G: Infra And Deployment Receipts:** Owns infra scripts, AWS workflow/runbook files, and verification receipts.
- **Reviewer H:** Reviews final commit split and blocks unsafe staging.

---

### Task 1: Coordinator Baseline Snapshot

**Files:**
- Create: none
- Modify: none
- Test: command-only

- [ ] **Step 1: Confirm repo root**

Run:

```powershell
git rev-parse --show-toplevel
```

Expected:

```text
C:/Users/nolan/CascadeProjects/fincept-terminal
```

- [ ] **Step 2: Fetch remote refs**

Run:

```powershell
git fetch origin --prune
```

Expected: command exits `0`.

- [ ] **Step 3: Confirm branch and upstream**

Run:

```powershell
git status --short --branch
git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>$null; if ($LASTEXITCODE -ne 0) { 'NO_UPSTREAM' }
```

Expected:

```text
## codex/portfolio-optimizer-core
NO_UPSTREAM
```

- [ ] **Step 4: Confirm no GitHub merge is required**

Run:

```powershell
git rev-list --left-right --count origin/main...HEAD
git log --oneline HEAD..origin/main --max-count=20
```

Expected:

```text
0    <some positive number>
```

The second command should print no commits. If it prints commits, stop and ask for a merge/rebase decision.

- [ ] **Step 5: Set upstream only if branch still has none**

Run:

```powershell
git branch --set-upstream-to=origin/codex/portfolio-optimizer-core codex/portfolio-optimizer-core
git status --short --branch
```

Expected: status shows the branch tracking `origin/codex/portfolio-optimizer-core`.

---

### Task 2: Scout A GitHub And PR Status

**Files:**
- Create: none
- Modify: none
- Test: command-only

- [ ] **Step 1: Check current branch remote parity**

Run:

```powershell
git rev-list --left-right --count origin/codex/portfolio-optimizer-core...HEAD
```

Expected before new commits:

```text
0    0
```

- [ ] **Step 2: Check PR state**

Run:

```powershell
gh pr list --repo AIRYDER/fincept --head codex/portfolio-optimizer-core --state all --json number,state,title,headRefName,baseRefName,url,mergeStateStatus --limit 10
```

Expected before PR creation:

```json
[]
```

- [ ] **Step 3: Report branch conclusion**

Return exactly this shape to the coordinator:

```markdown
## GitHub Status
- Merge needed from origin/main: no
- Local branch tracks upstream: yes/no
- Remote branch exists: yes/no
- Existing PR: yes/no, URL if yes
- Safe to stage files now: no, secret/artifact scout must run first
```

---

### Task 3: Scout B Secret And Artifact Risk Sweep

**Files:**
- Create: none
- Modify: none
- Test: command-only

- [ ] **Step 1: List dirty tracked files**

Run:

```powershell
git diff --name-only
```

Expected currently includes:

```text
apps/dashboard/src/app/globals.css
apps/dashboard/src/lib/design-tokens.test.ts
apps/dashboard/src/lib/design-tokens.ts
apps/dashboard/tailwind.config.ts
docs/ROADMAP.md
featuresmenu.md
services/api/src/api/routes/quant_foundry.py
services/api/tests/test_promotion_endpoints.py
services/quant_foundry/src/quant_foundry/gateway.py
services/quant_foundry/src/quant_foundry/registry.py
services/quant_foundry/tests/test_dossier.py
```

- [ ] **Step 2: List untracked files without dumping contents**

Run:

```powershell
git ls-files -o --exclude-standard
```

Expected: many paths. Do not open large generated directories unless the coordinator asks.

- [ ] **Step 3: Scan filenames for obvious secret/risky patterns**

Run:

```powershell
git ls-files -o --exclude-standard | Select-String -Pattern '\.env|\.pem$|\.key$|\.p12$|\.pfx$|id_rsa|id_ed25519|credentials|service-account|secrets|token|password|\.npmrc|\.pypirc|ssh-key|git-credentials' -CaseSensitive:$false
git diff --name-only | Select-String -Pattern '\.env|\.pem$|\.key$|\.p12$|\.pfx$|id_rsa|id_ed25519|credentials|service-account|secrets|token|password|\.npmrc|\.pypirc|ssh-key|git-credentials' -CaseSensitive:$false
```

Expected: no files that should be staged. If any are found, mark them `DO_NOT_COMMIT` and tell the coordinator.

- [ ] **Step 4: Classify untracked paths**

Return a table with these buckets:

```markdown
| Bucket | Commit? | Paths |
|---|---:|---|
| Product source | yes/no | ... |
| Tests | yes/no | ... |
| Docs/runbooks | yes/no | ... |
| CI/infra | yes/no | ... |
| Verification receipts | maybe | ... |
| Runtime/generated | no | ... |
| Local agent/tooling | no | ... |
| Secret-risk | no | ... |
```

Use these default `no` buckets unless proven otherwise:

```text
.worktrees/
.omo/
.opencode/
.playwright-cli/
reports/quant-foundry/
reports/training-stage/
data/datasets/
clipboard-*.png
session-db.md
```

---

### Task 4: Worker C Ignore Hygiene

**Files:**
- Modify: `.gitignore`
- Test: command-only

- [ ] **Step 1: Inspect existing ignore rules**

Run:

```powershell
Get-Content .gitignore
```

Expected: file exists. If it does not exist, stop and report.

- [ ] **Step 2: Add only generated/local ignore rules approved by Scout B**

Allowed candidate lines:

```gitignore
.omo/
.opencode/
.playwright-cli/
.worktrees/
data/datasets/
reports/quant-foundry/
reports/training-stage/
clipboard-*.png
session-db.md
```

Do not add broad rules like `reports/` or `data/`.

- [ ] **Step 3: Verify ignore behavior**

Run:

```powershell
git status --short --ignored
git check-ignore -v .omo/ .opencode/ .playwright-cli/ .worktrees/ reports/quant-foundry/ reports/training-stage/ data/datasets/ session-db.md
```

Expected: each approved generated path is ignored by the exact rule added.

- [ ] **Step 4: Commit ignore hygiene only after coordinator approval**

Run only after approval:

```powershell
git add .gitignore
git commit -m "chore(git): ignore local generated hygiene artifacts"
```

---

### Task 5: Worker D Quant Foundry Promotion Fix Commit

**Files:**
- Modify: `services/api/src/api/routes/quant_foundry.py`
- Modify: `services/api/tests/test_promotion_endpoints.py`
- Modify: `services/quant_foundry/src/quant_foundry/gateway.py`
- Modify: `services/quant_foundry/src/quant_foundry/registry.py`
- Modify: `services/quant_foundry/tests/test_dossier.py`
- Test: `services/quant_foundry/tests/test_dossier.py`
- Test: `services/api/tests/test_promotion_endpoints.py`
- Test: `services/quant_foundry/tests/test_shadow_dispatch.py`

- [ ] **Step 1: Confirm diff is scoped to the five files**

Run:

```powershell
git diff -- services/api/src/api/routes/quant_foundry.py services/api/tests/test_promotion_endpoints.py services/quant_foundry/src/quant_foundry/gateway.py services/quant_foundry/src/quant_foundry/registry.py services/quant_foundry/tests/test_dossier.py
```

Expected:
- Gateway updates dossier status only after an approved promotion receipt.
- Registry has `update_status`.
- Promotion endpoint tests assert approved status becomes `shadow_approved`.
- Insufficient evidence leaves the model as `candidate`.
- Route uses `HTTP_422_UNPROCESSABLE_CONTENT`.

- [ ] **Step 2: Run focused test slice**

Run:

```powershell
uv run pytest services/quant_foundry/tests/test_dossier.py services/api/tests/test_promotion_endpoints.py services/quant_foundry/tests/test_shadow_dispatch.py -q
```

Expected:

```text
57 passed
```

- [ ] **Step 3: Run compile check**

Run:

```powershell
uv run python -m compileall -q services/api/src/api/routes/quant_foundry.py services/quant_foundry/src/quant_foundry/gateway.py services/quant_foundry/src/quant_foundry/registry.py
```

Expected: no output and exit `0`.

- [ ] **Step 4: Stage only these five files**

Run:

```powershell
git add services/api/src/api/routes/quant_foundry.py services/api/tests/test_promotion_endpoints.py services/quant_foundry/src/quant_foundry/gateway.py services/quant_foundry/src/quant_foundry/registry.py services/quant_foundry/tests/test_dossier.py
git diff --cached --name-only
```

Expected:

```text
services/api/src/api/routes/quant_foundry.py
services/api/tests/test_promotion_endpoints.py
services/quant_foundry/src/quant_foundry/gateway.py
services/quant_foundry/src/quant_foundry/registry.py
services/quant_foundry/tests/test_dossier.py
```

- [ ] **Step 5: Commit**

Run:

```powershell
git commit -m "fix(quant-foundry): persist approved promotion status"
```

---

### Task 6: Worker E Dashboard Design Token Commit

**Files:**
- Modify: `apps/dashboard/src/app/globals.css`
- Modify: `apps/dashboard/src/lib/design-tokens.test.ts`
- Modify: `apps/dashboard/src/lib/design-tokens.ts`
- Modify: `apps/dashboard/tailwind.config.ts`
- Consider: untracked dashboard widget/component/test files under `apps/dashboard/src/`
- Test: dashboard package scripts

- [ ] **Step 1: Inspect dashboard diff**

Run:

```powershell
git diff -- apps/dashboard/src/app/globals.css apps/dashboard/src/lib/design-tokens.test.ts apps/dashboard/src/lib/design-tokens.ts apps/dashboard/tailwind.config.ts
```

Expected: only design token or Tailwind-related behavior.

- [ ] **Step 2: Classify untracked dashboard files**

Run:

```powershell
git ls-files -o --exclude-standard apps/dashboard
```

Expected: dashboard scripts, widgets, route files, and docs. Put each path into one of:

```text
design-token commit
watchlist/trading widgets commit
news-impact commit
dashboard docs commit
do not commit
```

- [ ] **Step 3: Run dashboard focused tests**

Run:

```powershell
Push-Location apps/dashboard
npm run test:shadow-news-impact
npm run test:source-health
npm run test:strategy-readiness
Pop-Location
```

Expected: all three scripts pass. If `package.json` lacks one script, stop and report the missing script instead of inventing another.

- [ ] **Step 4: Stage only coherent dashboard files**

Run only after classification:

```powershell
git add apps/dashboard/src/app/globals.css apps/dashboard/src/lib/design-tokens.test.ts apps/dashboard/src/lib/design-tokens.ts apps/dashboard/tailwind.config.ts
git diff --cached --stat
```

Expected: staged files match the chosen dashboard commit only.

- [ ] **Step 5: Commit**

Run:

```powershell
git commit -m "feat(dashboard): add design token safety coverage"
```

---

### Task 7: Worker F Docs And Roadmap Commit

**Files:**
- Modify: `docs/ROADMAP.md`
- Modify: `featuresmenu.md`
- Consider: `docs/RELEASE_HYGIENE.md`
- Consider: `docs/RUNPOD_LIVE_TRAINING_SESSION_SUMMARY.md`
- Consider: `docs/SYSTEM_IMPROVEMENT_REPORT.md`
- Consider: `docs/project-understanding/`
- Consider: `docs/quant-ml-audit/`
- Test: `git diff --check`

- [ ] **Step 1: Inspect tracked docs diff**

Run:

```powershell
git diff -- docs/ROADMAP.md featuresmenu.md
```

Expected: roadmap/status updates should separate shipped work from remaining work.

- [ ] **Step 2: Classify untracked docs**

Run:

```powershell
git ls-files -o --exclude-standard docs
```

Expected: docs only. Separate canonical docs from scratch notes and duplicate reports.

- [ ] **Step 3: Check docs for overclaiming**

Search:

```powershell
Select-String -Path docs/ROADMAP.md,featuresmenu.md -Pattern "complete|shipped|validated|passed|production|live" -CaseSensitive:$false
```

For each claim, verify there is a command, receipt, or file reference. If no proof exists, rewrite the claim as pending.

- [ ] **Step 4: Run whitespace check**

Run:

```powershell
git diff --check -- docs/ROADMAP.md featuresmenu.md
```

Expected: no output and exit `0`.

- [ ] **Step 5: Stage only canonical docs**

Run:

```powershell
git add docs/ROADMAP.md featuresmenu.md
git diff --cached --stat
```

Expected: only the docs selected for this commit are staged.

- [ ] **Step 6: Commit**

Run:

```powershell
git commit -m "docs: refresh Fincept roadmap hygiene status"
```

---

### Task 8: Worker G Infra And Verification Receipts

**Files:**
- Consider: `.github/workflows/aws-iac-validate.yml`
- Consider: `infra/`
- Consider: `scripts/aws_postapply_verify.ps1`
- Consider: `scripts/aws_preflight.ps1`
- Consider: `scripts/aws_receipt.ps1`
- Consider: `scripts/verification-receipt.ps1`
- Consider: `reports/verification/`
- Test: IaC and script validation commands

- [ ] **Step 1: List infra and receipt paths**

Run:

```powershell
git ls-files -o --exclude-standard .github infra scripts reports/verification
git diff --name-only -- .github infra scripts reports/verification
```

Expected: only infra, scripts, and verification receipt files.

- [ ] **Step 2: Identify generated versus canonical receipts**

Use this rule:
- Commit `.github/workflows/*.yml`, `infra/**`, and reusable `scripts/*.ps1` when reviewed.
- Commit `reports/verification/*.md` only if it is a deliberate human-readable release receipt.
- Commit `reports/verification/*.json` only if the project already treats JSON receipts as source-controlled evidence.
- Do not commit transient logs, local machine dumps, or credential-bearing outputs.

- [ ] **Step 3: Scan receipt files for secrets before staging**

Run:

```powershell
Select-String -Path reports/verification/*,scripts/*.ps1,.github/workflows/*.yml -Pattern "AKIA|ASIA|aws_secret|secret_access|BEGIN .*PRIVATE KEY|ghp_|gho_|ghs_|sk_live_|sk-proj-|xoxb-|password|token" -CaseSensitive:$false
```

Expected: no secret values. If any match looks real, stop and report `DO_NOT_COMMIT`.

- [ ] **Step 4: Run script parse checks**

Run:

```powershell
powershell -NoProfile -Command "[scriptblock]::Create((Get-Content scripts/aws_preflight.ps1 -Raw)) | Out-Null"
powershell -NoProfile -Command "[scriptblock]::Create((Get-Content scripts/aws_postapply_verify.ps1 -Raw)) | Out-Null"
powershell -NoProfile -Command "[scriptblock]::Create((Get-Content scripts/aws_receipt.ps1 -Raw)) | Out-Null"
powershell -NoProfile -Command "[scriptblock]::Create((Get-Content scripts/verification-receipt.ps1 -Raw)) | Out-Null"
```

Expected: all commands exit `0`.

- [ ] **Step 5: Stage reviewed infra only**

Run after classification:

```powershell
git add .github/workflows/aws-iac-validate.yml infra scripts/aws_postapply_verify.ps1 scripts/aws_preflight.ps1 scripts/aws_receipt.ps1 scripts/verification-receipt.ps1
git diff --cached --stat
```

Expected: no runtime logs, no secrets, no unrelated reports.

- [ ] **Step 6: Commit**

Run:

```powershell
git commit -m "ci(infra): add AWS verification workflow receipts"
```

---

### Task 9: Reviewer H Final Commit Split Review

**Files:**
- Create: none
- Modify: none
- Test: command-only

- [ ] **Step 1: Confirm no risky paths are staged**

Run:

```powershell
git diff --cached --name-only | Select-String -Pattern '\.env|\.pem$|\.key$|\.p12$|\.pfx$|id_rsa|id_ed25519|credentials|service-account|secrets|token|password|\.npmrc|\.pypirc|\.worktrees|\.omo|\.opencode|\.playwright-cli|reports/quant-foundry|reports/training-stage|data/datasets|clipboard-' -CaseSensitive:$false
```

Expected: no output.

- [ ] **Step 2: Confirm worktree leftovers are understood**

Run:

```powershell
git status --short
```

Expected: any remaining files are either intentionally deferred or ignored. If there are unknown untracked files, stop and classify them before push.

- [ ] **Step 3: Check recent commits**

Run:

```powershell
git log --oneline --decorate --max-count=10
```

Expected: recent commits are coherent and separately reviewable.

- [ ] **Step 4: Run final low-cost hygiene gate**

Run:

```powershell
git diff --check
```

Expected: no output and exit `0`.

---

### Task 10: Coordinator Push And PR

**Files:**
- Create: none
- Modify: none
- Test: GitHub command-only

- [ ] **Step 1: Confirm branch tracking**

Run:

```powershell
git status --short --branch
```

Expected: branch tracks `origin/codex/portfolio-optimizer-core`.

- [ ] **Step 2: Push branch**

Run:

```powershell
git push
```

Expected: push exits `0`. If rejected, run `git fetch origin` and report the rejection; do not force-push.

- [ ] **Step 3: Open PR only after push succeeds**

Run:

```powershell
gh pr create --repo AIRYDER/fincept --base main --head codex/portfolio-optimizer-core --title "chore: prepare Fincept branch hygiene for review" --body-file docs/superpowers/plans/2026-06-26-fincept-github-hygiene-swarm.md
```

Expected: GitHub returns a PR URL.

- [ ] **Step 4: Verify PR status**

Run:

```powershell
gh pr status --repo AIRYDER/fincept
```

Expected: PR appears under created-by-you.

---

## Stop Conditions

Any worker must stop and report instead of continuing when:

- A secret-like file is found.
- A task requires staging more than its allowed paths.
- A generated directory contains files that look source-like but have no owner.
- `origin/main` gains commits not contained in this branch.
- Tests fail for reasons unrelated to the worker's scoped change.
- A command would need destructive Git cleanup.

## Suggested Commit Order

1. `chore(git): ignore local generated hygiene artifacts`
2. `fix(quant-foundry): persist approved promotion status`
3. `feat(dashboard): add design token safety coverage`
4. `docs: refresh Fincept roadmap hygiene status`
5. `ci(infra): add AWS verification workflow receipts`

## Self-Review

- Spec coverage: plan covers GitHub merge status, upstream setup, dirty tree classification, secret sweep, ignore hygiene, scoped commits, tests, push, and PR creation.
- Placeholder scan: no task uses forbidden placeholder language or unspecified test instructions.
- Type/path consistency: all paths are repo-relative under `C:\Users\nolan\CascadeProjects\fincept-terminal`; the Quant Foundry validation commands match the last known passing focused test slice.
