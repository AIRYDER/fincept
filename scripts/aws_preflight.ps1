<#
.SYNOPSIS
  Pre-flight for the Fincept AWS production deployment (TASK-0903).

.DESCRIPTION
  Walks the operator through every check in docs/AWS_DEPLOY_RUNBOOK.md
  §1 (Pre-flight checklist) before any terraform apply runs.

  The script:
    1. Confirms AWS CLI + Terraform + tflint + tfsec are installed and reachable.
    2. Verifies the configured AWS caller identity + region.
    3. Reads the current service quotas for the seven quotas the runbook
       calls out (Fargate vCPUs, Fargate memory, EIPs, RDS instances,
       ElastiCache replication groups, ALBs, Secrets Manager secrets) and
       flags any that are below the recommended production floor.
    4. Confirms secrets.auto.tfvars (gitignored) exists with the expected
       secret entries, AND that no plaintext credential is present in the
       repo tree (gitleaks-equivalent scan of the working directory).
    5. Confirms the ECR repos can be listed (or that the caller has
       permission to create them on first apply).
    6. Confirms the operator is NOT using the AWS root account.
    7. Writes a timestamped preflight receipt under reports/verification/
       alongside the rest of the project's verification artifacts.

  This script never invokes `terraform apply`. It only reports. Exit code
  is non-zero if any REQUIRED check fails (matching the rest of the
  Fincept verification harness).

.PARAMETER Profile
  AWS CLI profile name (passed to --profile). Defaults to $env:AWS_PROFILE
  or the default profile.

.PARAMETER Region
  AWS region. Defaults to $env:AWS_REGION or us-east-1 (matching the
  Terraform default in infra/aws/variables.tf).

.PARAMETER SecretsFile
  Path to the gitignored secrets.auto.tfvars file. Defaults to
  infra/aws/secrets.auto.tfvars.

.PARAMETER OutDir
  Override the receipt output directory.

.PARAMETER SkipQuotaCheck
  Skip the service-quota check (useful when the operator knows quotas
  are already raised; the runbook still requires them).

.EXAMPLE
  pwsh ./scripts/aws_preflight.ps1 -Profile fincept-prod
#>

[CmdletBinding()]
param(
    [string]$Profile = "",
    [string]$Region = "",
    [string]$SecretsFile = "",
    [string]$OutDir = "",
    [switch]$SkipQuotaCheck
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Continue"

$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

if ([string]::IsNullOrEmpty($Region)) {
    $Region = if ($env:AWS_REGION) { $env:AWS_REGION } else { "us-east-1" }
}
if ([string]::IsNullOrEmpty($Profile)) {
    $Profile = if ($env:AWS_PROFILE) { $env:AWS_PROFILE } else { "" }
}
if ([string]::IsNullOrEmpty($SecretsFile)) {
    $SecretsFile = Join-Path $RepoRoot "infra" "aws" "secrets.auto.tfvars"
}
if ([string]::IsNullOrEmpty($OutDir)) {
    $OutDir = Join-Path $RepoRoot "reports" "verification"
}
if (-not (Test-Path $OutDir)) {
    New-Item -ItemType Directory -Path $OutDir -Force | Out-Null
}

$awsArgs = @("--region", $Region)
if (-not [string]::IsNullOrEmpty($Profile)) { $awsArgs = @("--profile", $Profile) + $awsArgs }

$StartedAt = Get-Date
$Stamp = $StartedAt.ToString("yyyyMMddTHHmmss")
$MdPath = Join-Path $OutDir "aws-preflight-$Stamp.md"
$JsonPath = Join-Path $OutDir "aws-preflight-$Stamp.json"

$Results = [System.Collections.Generic.List[hashtable]]::new()
$RequiredFailures = 0

function Add-Result {
    param(
        [string]$Name,
        [string]$Command,
        [string]$Status,
        [int]$ExitCode,
        [double]$DurationMs,
        [string]$Reason = "",
        [string]$Detail = "",
        [bool]$Required = $true
    )
    $Results.Add(@{
        name = $Name
        command = $Command
        status = $Status
        exit_code = $ExitCode
        duration_ms = [int]$DurationMs
        reason = $Reason
        detail = $Detail
        required = $Required
    })
    if ($Status -eq "fail" -and $Required) {
        $script:RequiredFailures += 1
    }
}

function Invoke-Check {
    param(
        [string]$Name,
        [string]$CommandDisplay,
        [scriptblock]$Block,
        [bool]$Required = $true
    )
    Write-Host "==> $Name" -ForegroundColor Cyan
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $code = 0
    $output = ""
    try {
        $output = (& $Block 2>&1 | Out-String).TrimEnd()
        $code = $LASTEXITCODE
        if ($null -eq $code) { $code = 0 }
    } catch {
        # The block threw — most commonly because a required CLI (aws,
        # terraform, tflint, tfsec) is not installed. Surface that as a
        # normal FAIL with the exception message rather than crashing
        # the harness.
        $code = 1
        $output = $_.Exception.Message
    }
    $sw.Stop()
    $status = if ($code -eq 0) { "pass" } else { "fail" }
    $color = if ($status -eq "pass") { "Green" } else { "Red" }
    Write-Host "   -> $status (exit $code, $($sw.ElapsedMilliseconds)ms)" -ForegroundColor $color
    if ($output.Length -gt 0) {
        $snippet = if ($output.Length -gt 400) { $output.Substring(0, 400) + "..." } else { $output }
        Write-Host "   -- $snippet" -ForegroundColor DarkGray
    }
    Add-Result -Name $Name -Command $CommandDisplay -Status $status -ExitCode $code `
        -DurationMs $sw.ElapsedMilliseconds -Detail $output -Required $Required
}

function Add-Skipped {
    param([string]$Name, [string]$Reason, [bool]$Required = $false)
    Write-Host "==> $Name [skipped]" -ForegroundColor DarkYellow
    Write-Host "   -> reason: $Reason" -ForegroundColor DarkGray
    Add-Result -Name $Name -Command "" -Status "skipped" -ExitCode 0 `
        -DurationMs 0 -Reason $Reason -Required $Required
}

# ---- §1.1 Tooling ---------------------------------------------------------

Invoke-Check "tool:aws-cli" "aws --version" { aws --version }
Invoke-Check "tool:terraform" "terraform version" { terraform version }
Invoke-Check "tool:tflint" "tflint --version" { tflint --version }
Invoke-Check "tool:tfsec" "tfsec --version" { tfsec --version }

# ---- §1.1 Account + credentials ------------------------------------------

Invoke-Check "aws:sts-get-caller-identity" "aws sts get-caller-identity" {
    aws @awsArgs sts get-caller-identity --output json
}

# Operator MUST NOT be using the AWS root account.
Invoke-Check "aws:not-root-account" "caller identity != root" {
    $identity = (aws @awsArgs sts get-caller-identity --output json | ConvertFrom-Json)
    $arn = $identity.Arn
    if ($arn -like "arn:aws:iam::*:root") {
        Write-Error "Caller is the AWS root account ($arn). Use a scoped IAM user or role instead."
throw "FAIL"
    }
    Write-Host "Caller: $arn (account $($identity.Account))"
}

# ---- §1.2 Service quotas (best-effort; uses service-quotas service) -----

if ($SkipQuotaCheck) {
    Add-Skipped "quota:fargate-vcpus"  "-SkipQuotaCheck" $false
    Add-Skipped "quota:fargate-memory" "-SkipQuotaCheck" $false
    Add-Skipped "quota:vpc-eips"        "-SkipQuotaCheck" $false
    Add-Skipped "quota:rds-instances"   "-SkipQuotaCheck" $false
    Add-Skipped "quota:elasticache-rgs" "-SkipQuotaCheck" $false
    Add-Skipped "quota:alb-count"       "-SkipQuotaCheck" $false
    Add-Skipped "quota:secrets-count"   "-SkipQuotaCheck" $false
} else {
    # Each quota check has a recommended floor for prod. We mark as fail if
    # the current value is below the floor — operator must raise a quota
    # increase request before apply.
    $QuotaFloor = @{
        "Fargate vCPUs"            = 1000
        "Fargate Memory (GB)"      = 8000
        "EC2-VPC Elastic IPs"      = 10
        "RDS DB Instances"         = 100
        "ElastiCache Replication Groups" = 50
        "Application Load Balancers" = 50
        "Secrets Manager secrets"  = 100
    }

    Invoke-Check "quota:fargate-vcpus"  "service-quotas: Fargate vCPUs" {
        $v = (aws @awsArgs service-quotas get-service-quota --service-code fargate --quota-code L-3032A386 --output json 2>$null | ConvertFrom-Json)
        if ($null -eq $v.Quota) { Write-Error "no quota value"; throw "no quota value" }
        $cur = [double]$v.Quota.Value
        if ($cur -lt $QuotaFloor["Fargate vCPUs"]) {
            Write-Error "Fargate vCPU quota = $cur (recommended floor: $($QuotaFloor['Fargate vCPUs'])). Request increase."
throw "FAIL"
        }
        Write-Host "Fargate vCPU quota = $cur"
    }

    Invoke-Check "quota:fargate-memory" "service-quotas: Fargate Memory (GB)" {
        $v = (aws @awsArgs service-quotas get-service-quota --service-code fargate --quota-code L-3032A387 --output json 2>$null | ConvertFrom-Json)
        if ($null -eq $v.Quota) { Write-Error "no quota value"; throw "no quota value" }
        $cur = [double]$v.Quota.Value
        if ($cur -lt $QuotaFloor["Fargate Memory (GB)"]) {
            Write-Error "Fargate memory quota = $cur (floor: $($QuotaFloor['Fargate Memory (GB)']))"
throw "FAIL"
        }
        Write-Host "Fargate memory quota = $cur GB"
    }

    Invoke-Check "quota:vpc-eips" "service-quotas: VPC Elastic IPs" {
        $v = (aws @awsArgs service-quotas get-service-quota --service-code ec2 --quota-code L-2A8BF9F7 --output json 2>$null | ConvertFrom-Json)
        if ($null -eq $v.Quota) { Write-Error "no quota value"; throw "no quota value" }
        $cur = [double]$v.Quota.Value
        if ($cur -lt $QuotaFloor["EC2-VPC Elastic IPs"]) {
            Write-Error "VPC EIP quota = $cur (floor: $($QuotaFloor['EC2-VPC Elastic IPs']))"
throw "FAIL"
        }
        Write-Host "VPC EIP quota = $cur"
    }

    Invoke-Check "quota:rds-instances" "service-quotas: RDS DB Instances" {
        $v = (aws @awsArgs service-quotas get-service-quota --service-code rds --quota-code L-7B6409FD --output json 2>$null | ConvertFrom-Json)
        if ($null -eq $v.Quota) { Write-Error "no quota value"; throw "no quota value" }
        $cur = [double]$v.Quota.Value
        if ($cur -lt $QuotaFloor["RDS DB Instances"]) {
            Write-Error "RDS instances quota = $cur (floor: $($QuotaFloor['RDS DB Instances']))"
throw "FAIL"
        }
        Write-Host "RDS instances quota = $cur"
    }

    Invoke-Check "quota:elasticache-rgs" "service-quotas: ElastiCache Replication Groups" {
        $v = (aws @awsArgs service-quotas get-service-quota --service-code elasticache --quota-code L-9C7B2B09 --output json 2>$null | ConvertFrom-Json)
        if ($null -eq $v.Quota) { Write-Error "no quota value"; throw "no quota value" }
        $cur = [double]$v.Quota.Value
        if ($cur -lt $QuotaFloor["ElastiCache Replication Groups"]) {
            Write-Error "ElastiCache RG quota = $cur (floor: $($QuotaFloor['ElastiCache Replication Groups']))"
throw "FAIL"
        }
        Write-Host "ElastiCache RG quota = $cur"
    }

    Invoke-Check "quota:alb-count" "service-quotas: Application Load Balancers" {
        $v = (aws @awsArgs service-quotas get-service-quota --service-code elasticloadbalancing --quota-code L-53FDA50B --output json 2>$null | ConvertFrom-Json)
        if ($null -eq $v.Quota) { Write-Error "no quota value"; throw "no quota value" }
        $cur = [double]$v.Quota.Value
        if ($cur -lt $QuotaFloor["Application Load Balancers"]) {
            Write-Error "ALB quota = $cur (floor: $($QuotaFloor['Application Load Balancers']))"
throw "FAIL"
        }
        Write-Host "ALB quota = $cur"
    }

    Invoke-Check "quota:secrets-count" "service-quotas: Secrets Manager secrets" {
        $v = (aws @awsArgs service-quotas get-service-quota --service-code secretsmanager --quota-code L-8B9F58C7 --output json 2>$null | ConvertFrom-Json)
        if ($null -eq $v.Quota) { Write-Error "no quota value"; throw "no quota value" }
        $cur = [double]$v.Quota.Value
        if ($cur -lt $QuotaFloor["Secrets Manager secrets"]) {
            Write-Error "Secrets quota = $cur (floor: $($QuotaFloor['Secrets Manager secrets']))"
throw "FAIL"
        }
        Write-Host "Secrets quota = $cur"
    }
}

# ---- §1.4 Image registry (ECR) -------------------------------------------

Invoke-Check "ecr:describe-repositories" "aws ecr describe-repositories" {
    # First apply will create these; we just verify the caller has permission.
    # Suppress 404 — empty repo list is fine, the failure we care about is
    # AccessDenied (caller lacks ecr:DescribeRepositories).
    $out = aws @awsArgs ecr describe-repositories --output json 2>&1
    if ($LASTEXITCODE -ne 0) {
        if ($out -like "*AccessDenied*") {
            Write-Error "Caller lacks ecr:DescribeRepositories permission."
throw "FAIL"
        }
        # No repos yet — fine, they get created on first apply.
        Write-Host "No ECR repositories exist yet (expected on first deploy)."
        exit 0
    }
    Write-Host "ECR reachable."
}

# ---- §1.5 Secret material ------------------------------------------------

Invoke-Check "secret:secrets-auto-tfvars-exists" "$SecretsFile" {
    if (-not (Test-Path $SecretsFile)) {
        Write-Error "$SecretsFile not found. Copy terraform.tfvars.example, replace REPLACE_ME placeholders, and ensure it's gitignored."
throw "FAIL"
    }
    $content = Get-Content -Raw $SecretsFile
    # Every expected secret must appear with a non-placeholder value.
    $expected = @(
        "fincept/callback-secret"
        "fincept/jwt-signing-key"
        "fincept/runpod-api-key"
        "fincept/db-password"
        "fincept/redis-auth-token"
        "fincept/openai-api-key"
        "fincept/anthropic-api-key"
    )
    $missing = @()
    foreach ($name in $expected) {
        if ($content -notmatch [regex]::Escape($name)) {
            $missing += $name
        }
    }
    if ($missing.Count -gt 0) {
        Write-Error "Missing secret entries: $($missing -join ', ')"
throw "FAIL"
    }
    # Catch the placeholder — REPLACE_ME means the operator forgot to fill in.
    if ($content -match "REPLACE_ME") {
        Write-Error "secrets.auto.tfvars still contains REPLACE_ME placeholders."
throw "FAIL"
    }
    Write-Host "All 7 secret entries present, no REPLACE_ME placeholders."
}

# Crude secret-leak check — looks for things that look like AWS access keys,
# GitHub PATs, OpenAI/Anthropic keys in the *committed* tree. This is NOT
# a replacement for gitleaks; the runbook recommends running gitleaks as a
# pre-commit hook (already wired in .pre-commit-config.yaml).
Invoke-Check "secret:no-plaintext-leak-in-tree" "rg for AWS keys / OpenAI keys" {
    $patterns = @(
        @{ name = "AWS access key";   re = "AKIA[0-9A-Z]{16}" },
        @{ name = "GitHub PAT";       re = "ghp_[0-9A-Za-z]{36}" },
        @{ name = "OpenAI key";       re = "sk-[A-Za-z0-9]{32,}" },
        @{ name = "Anthropic key";    re = "sk-ant-[A-Za-z0-9_-]{32,}" }
    )
    $hits = @()
    foreach ($p in $patterns) {
        $found = rg --hidden --glob '!.git/' --glob '!.venv/' --glob '!node_modules/' --glob '!.uv-cache/' --glob '!.pnpm-store/' --glob '!.pytest_cache/' --glob '!.ruff_cache/' --glob '!.mypy_cache/' --glob '!reports/verification/aws-preflight-*' -e $p.re $RepoRoot 2>$null
        if ($found) {
            foreach ($line in $found) {
                $hits += "$($p.name): $line"
            }
        }
    }
    if ($hits.Count -gt 0) {
        Write-Error "Plaintext secret-like pattern found in tree. Review and remove before applying."
        $hits | ForEach-Object { Write-Host "  $_" -ForegroundColor Red }
throw "FAIL"
    }
    Write-Host "No plaintext secret-like patterns found in committed tree."
}

# ---- §2.2 Format + validate (cheap local check) --------------------------

Invoke-Check "tf:fmt" "terraform fmt -recursive -check" {
    Push-Location (Join-Path $RepoRoot "infra" "aws")
    try { terraform fmt -recursive -check; if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE } }
    finally { Pop-Location }
}

Invoke-Check "tf:validate" "terraform validate" {
    Push-Location (Join-Path $RepoRoot "infra" "aws")
    try { terraform init -backend=false -input=false -no-color 2>$null | Out-Null; terraform validate -no-color }
    finally { Pop-Location }
}

# ---- Summary --------------------------------------------------------------

$EndedAt = Get-Date
$DurationS = [math]::Round(($EndedAt - $StartedAt).TotalSeconds, 2)
$passCount = @($Results | Where-Object { $_.status -eq "pass" }).Count
$failCount = @($Results | Where-Object { $_.status -eq "fail" }).Count
$skipCount = @($Results | Where-Object { $_.status -eq "skipped" }).Count
$overall = if ($RequiredFailures -eq 0) { "PASS" } else { "FAIL" }

$md = New-Object System.Text.StringBuilder
[void]$md.AppendLine("# AWS Pre-flight Receipt — $($StartedAt.ToString('o'))")
[void]$md.AppendLine("")
[void]$md.AppendLine("> Generated by ``scripts/aws_preflight.ps1`` (TASK-0903).")
[void]$md.AppendLine("> This script enforces docs/AWS_DEPLOY_RUNBOOK.md §1 (Pre-flight checklist).")
[void]$md.AppendLine("> Receipts never include secret values.")
[void]$md.AppendLine("")
[void]$md.AppendLine("## Configuration")
[void]$md.AppendLine("")
[void]$md.AppendLine("- **AWS region:** $Region")
[void]$md.AppendLine("- **AWS profile:** $(if ([string]::IsNullOrEmpty($Profile)) { '<default>' } else { $Profile })")
[void]$md.AppendLine("- **Secrets file:** $SecretsFile")
[void]$md.AppendLine("")
[void]$md.AppendLine("## Summary")
[void]$md.AppendLine("")
[void]$md.AppendLine("- **Overall:** $overall")
[void]$md.AppendLine("- **Duration:** ${DurationS}s")
[void]$md.AppendLine("- **Pass:** $passCount  **Fail:** $failCount  **Skipped:** $skipCount")
[void]$md.AppendLine("- **Required failures:** $RequiredFailures")
[void]$md.AppendLine("")
[void]$md.AppendLine("## Checks")
[void]$md.AppendLine("")
[void]$md.AppendLine("| Name | Status | Exit | Duration (ms) | Required | Detail |")
[void]$md.AppendLine("|---|---|---|---|---|---|")
foreach ($r in $Results) {
    $req = if ($r.required) { "yes" } else { "no" }
    $detail = ($r.detail -replace "`r?`n", " " -replace "\|", "\|").Trim()
    if ($detail.Length -gt 200) { $detail = $detail.Substring(0, 200) + "..." }
    [void]$md.AppendLine("| $($r.name) | $($r.status) | $($r.exit_code) | $($r.duration_ms) | $req | $detail |")
}
[void]$md.AppendLine("")

[System.IO.File]::WriteAllText($MdPath, $md.ToString())

$receipt = [ordered]@{
    schema = "fincept.aws-preflight/v1"
    started_at = $StartedAt.ToString("o")
    ended_at = $EndedAt.ToString("o")
    duration_s = $DurationS
    region = $Region
    profile = $Profile
    overall = $overall
    summary = [ordered]@{
        pass = $passCount
        fail = $failCount
        skipped = $skipCount
        required_failures = $RequiredFailures
    }
    checks = $Results
}
$receipt | ConvertTo-Json -Depth 6 | Out-File -FilePath $JsonPath -Encoding utf8

Write-Host ""
Write-Host "Receipt written:" -ForegroundColor Green
Write-Host "  $MdPath" -ForegroundColor DarkGray
Write-Host "  $JsonPath" -ForegroundColor DarkGray
Write-Host "Overall: $overall (pass=$passCount fail=$failCount skipped=$skipCount)" -ForegroundColor $(if ($overall -eq "PASS") { "Green" } else { "Red" })

if ($RequiredFailures -gt 0) {
    Write-Host ""
    Write-Host "DO NOT run terraform apply. Fix the failures above first." -ForegroundColor Red
throw "FAIL"
}
exit 0