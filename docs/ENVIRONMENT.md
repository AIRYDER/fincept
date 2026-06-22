# Environment Configuration — Quant Foundry

## Artifact Import URI Schemes

The Quant Foundry artifact importer (`quant_foundry.artifacts.import_artifact`)
supports the following URI schemes:

### `file://` (local)

- **Use:** Local development, testing, and MVP.
- **Example:** `file:///C:/models/model.pkl`
- **Security:** Path traversal (`..` segments) is rejected. The path must
  resolve to an existing file.

### `s3://` (object storage)

- **Use:** Production — RunPod workers write model artifacts to S3.
- **Example:** `s3://my-bucket/models/model.pkl`
- **Security:**
  - Path traversal (`..` segments in the key) is rejected.
  - S3 reads are delegated to an injected `s3_reader` callable — the
    artifact module has no AWS/boto3 coupling, and credentials stay
    isolated in the caller.
  - The caller is responsible for providing an `s3_reader` that handles
    authentication (e.g., via IAM roles, STS tokens, or environment
    credentials). The artifact module never sees AWS credentials.

### Disallowed schemes

- `http://`, `https://`, `ftp://`, and arbitrary schemes are **rejected**
  so a malicious worker cannot point Fincept at an attacker-controlled URL.

## Artifact Import Security Controls

| Control | Parameter | Default | Description |
|---------|-----------|---------|-------------|
| URI scheme allowlist | — | `file`, `s3` | Only allowlisted schemes are accepted. |
| Path traversal rejection | — | enabled | `..` segments in paths/keys are rejected. |
| Size limit | `max_size_bytes` | `None` (no limit) | Artifacts exceeding this are rejected before hash verification. |
| Content type validation | `allowed_content_types` | `None` (no restriction) | File extension must be in the allowlist (e.g., `.pkl`, `.onnx`, `.pt`). |
| Hash verification | `expected_sha256` | required | Imported bytes must SHA-256 to the expected hash or the import fails closed. |
| Quarantine / staging | `quarantine_dir` | `None` | When provided, the artifact is copied to a staging path before hash verification. |
| Security receipts | — | enabled | Every rejection carries a `SecurityReceipt` on the exception for audit/persistence. |

## Environment Variables

The following environment variables are used by the Quant Foundry (not all
are required for MVP):

| Variable | Default | Description |
|----------|---------|-------------|
| `QUANT_FOUNDRY_MODE` | `local_mock` | `local_mock` or `runpod`. |
| `RUNPOD_API_KEY` | — | RunPod API key (only in `runpod` mode). |
| `AWS_ACCESS_KEY_ID` | — | AWS access key (for S3 artifact reads). |
| `AWS_SECRET_ACCESS_KEY` | — | AWS secret key (for S3 artifact reads). |
| `AWS_S3_BUCKET` | — | Default S3 bucket for artifact storage. |
| `AWS_REGION` | `us-east-1` | AWS region. |

**Note:** AWS credentials are NEVER stored in the artifact module or the
dossier registry. They are used only by the `s3_reader` callable provided
by the caller, which handles authentication externally.
