"""Tests for the URI allowlist module (T-2.3).

Covers every public function and the fail-closed behaviour required for
the production worker: localhost, private IPs, path traversal, system
paths and disallowed schemes/hosts are all rejected.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from quant_foundry.uri_allowlist import (
    URIAllowlistConfig,
    URIScheme,
    URIValidationResult,
    has_path_traversal,
    is_localhost,
    is_private_ip,
    is_under_root,
    redact_uri,
    validate_uri,
    validate_uris,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def prod_config() -> URIAllowlistConfig:
    """A representative production config."""
    return URIAllowlistConfig(
        allowed_schemes=[
            URIScheme.FILE,
            URIScheme.HTTP,
            URIScheme.HTTPS,
            URIScheme.S3,
            URIScheme.GS,
            URIScheme.AZBLOB,
            URIScheme.RUNPOD_VOLUME,
        ],
        allowed_volume_roots=["/workspace/data", "/workspace/artifacts"],
        allowed_object_hosts=[
            "s3.amazonaws.com",
            "storage.googleapis.com",
            "blob.core.windows.net",
        ],
        production_mode=True,
        allow_localhost=False,
        allow_arbitrary_http=False,
    )


@pytest.fixture
def dev_config() -> URIAllowlistConfig:
    """A permissive dev config that allows localhost and arbitrary HTTP."""
    return URIAllowlistConfig(
        allowed_schemes=[
            URIScheme.FILE,
            URIScheme.HTTP,
            URIScheme.HTTPS,
            URIScheme.S3,
        ],
        allowed_volume_roots=["/workspace/data"],
        allowed_object_hosts=["s3.amazonaws.com"],
        production_mode=False,
        allow_localhost=True,
        allow_arbitrary_http=True,
    )


# ---------------------------------------------------------------------------
# URIScheme enum
# ---------------------------------------------------------------------------


class TestURIScheme:
    def test_file_value(self) -> None:
        assert URIScheme.FILE == "file"

    def test_http_value(self) -> None:
        assert URIScheme.HTTP == "http"

    def test_https_value(self) -> None:
        assert URIScheme.HTTPS == "https"

    def test_s3_value(self) -> None:
        assert URIScheme.S3 == "s3"

    def test_gs_value(self) -> None:
        assert URIScheme.GS == "gs"

    def test_azblob_value(self) -> None:
        assert URIScheme.AZBLOB == "azblob"

    def test_runpod_volume_value(self) -> None:
        assert URIScheme.RUNPOD_VOLUME == "runpod_volume"

    def test_from_string(self) -> None:
        assert URIScheme("https") is URIScheme.HTTPS

    def test_unknown_string_raises(self) -> None:
        with pytest.raises(ValueError):
            URIScheme("ftp")


# ---------------------------------------------------------------------------
# URIAllowlistConfig
# ---------------------------------------------------------------------------


class TestURIAllowlistConfig:
    def test_default_construction(self) -> None:
        cfg = URIAllowlistConfig()
        assert URIScheme.FILE in cfg.allowed_schemes
        assert URIScheme.HTTPS in cfg.allowed_schemes
        assert cfg.production_mode is True
        assert cfg.allow_localhost is False

    def test_frozen(self) -> None:
        cfg = URIAllowlistConfig()
        with pytest.raises(ValidationError):
            cfg.production_mode = False  # type: ignore[misc]

    def test_extra_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            URIAllowlistConfig(unknown_field=1)  # type: ignore[call-arg]

    def test_empty_schemes_rejected(self) -> None:
        with pytest.raises(ValidationError):
            URIAllowlistConfig(allowed_schemes=[])

    def test_empty_volume_roots_rejected(self) -> None:
        with pytest.raises(ValidationError):
            URIAllowlistConfig(allowed_volume_roots=[])

    def test_custom_config(self) -> None:
        cfg = URIAllowlistConfig(
            allowed_schemes=[URIScheme.S3],
            allowed_volume_roots=["/data"],
            allowed_object_hosts=["my.host"],
        )
        assert cfg.allowed_schemes == [URIScheme.S3]
        assert cfg.allowed_object_hosts == ["my.host"]


# ---------------------------------------------------------------------------
# URIValidationResult
# ---------------------------------------------------------------------------


class TestURIValidationResult:
    def test_frozen(self) -> None:
        result = URIValidationResult(
            uri="file:///workspace/data/x",
            scheme=URIScheme.FILE,
            is_valid=True,
        )
        with pytest.raises(ValidationError):
            result.is_valid = False  # type: ignore[misc]

    def test_extra_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            URIValidationResult(
                uri="x",
                scheme=URIScheme.FILE,
                is_valid=True,
                extra=1,  # type: ignore[call-arg]
            )


# ---------------------------------------------------------------------------
# Helper predicates
# ---------------------------------------------------------------------------


class TestIsLocalhost:
    @pytest.mark.parametrize(
        "host",
        ["localhost", "127.0.0.1", "::1", "0.0.0.0", "Localhost", " 127.0.0.1 "],
    )
    def test_recognises_localhost(self, host: str) -> None:
        assert is_localhost(host) is True

    @pytest.mark.parametrize("host", ["example.com", "10.0.0.1", "", "169.254.1.1"])
    def test_rejects_non_localhost(self, host: str) -> None:
        assert is_localhost(host) is False

    def test_ipv6_brackets(self) -> None:
        assert is_localhost("[::1]") is True


class TestIsPrivateIp:
    @pytest.mark.parametrize(
        "host",
        [
            "10.0.0.1",
            "10.255.255.255",
            "172.16.0.1",
            "172.31.255.255",
            "192.168.1.1",
            "169.254.1.1",
        ],
    )
    def test_recognises_private(self, host: str) -> None:
        assert is_private_ip(host) is True

    @pytest.mark.parametrize(
        "host", ["8.8.8.8", "172.15.0.1", "172.32.0.1", "11.0.0.1", "example.com", ""]
    )
    def test_rejects_public(self, host: str) -> None:
        assert is_private_ip(host) is False

    def test_invalid_octets(self) -> None:
        assert is_private_ip("10.999.0.1") is False

    def test_non_ipv4(self) -> None:
        assert is_private_ip("::1") is False


class TestHasPathTraversal:
    @pytest.mark.parametrize(
        "path",
        ["../etc/passwd", "/workspace/../etc", "a/b/../../c", "/x/../y", "..\\etc\\passwd"],
    )
    def test_detects_traversal(self, path: str) -> None:
        assert has_path_traversal(path) is True

    @pytest.mark.parametrize("path", ["/workspace/data/x", "foo..bar", "/a/b/c", "", "normal.txt"])
    def test_no_traversal(self, path: str) -> None:
        assert has_path_traversal(path) is False


class TestIsUnderRoot:
    def test_under_root(self) -> None:
        assert is_under_root("/workspace/data/x.csv", ["/workspace/data"]) is True

    def test_root_itself(self) -> None:
        assert is_under_root("/workspace/data", ["/workspace/data"]) is True

    def test_outside_root(self) -> None:
        assert is_under_root("/etc/passwd", ["/workspace/data"]) is False

    def test_multiple_roots(self) -> None:
        assert (
            is_under_root("/workspace/artifacts/x", ["/workspace/data", "/workspace/artifacts"])
            is True
        )

    def test_empty_path(self) -> None:
        assert is_under_root("", ["/workspace/data"]) is False

    def test_empty_roots(self) -> None:
        assert is_under_root("/x", []) is False

    def test_traversal_safe(self) -> None:
        # ``..`` is resolved away by PurePosixPath.resolve, so a path
        # that *looks* under the root but escapes via ``..`` is rejected.
        assert is_under_root("/workspace/data/../../etc", ["/workspace/data"]) is False


# ---------------------------------------------------------------------------
# redact_uri
# ---------------------------------------------------------------------------


class TestRedactUri:
    def test_redacts_s3_credentials(self) -> None:
        assert redact_uri("s3://AKIA:SECRET@bucket/key") == "s3://***:***@bucket/key"

    def test_redacts_https_credentials(self) -> None:
        assert redact_uri("https://user:pass@host/path") == "https://***:***@host/path"

    def test_no_credentials_unchanged(self) -> None:
        assert redact_uri("s3://bucket/key") == "s3://bucket/key"

    def test_empty_uri(self) -> None:
        assert redact_uri("") == ""


# ---------------------------------------------------------------------------
# validate_uri - file://
# ---------------------------------------------------------------------------


class TestValidateFileUri:
    def test_approved_volume_root_accepted(self, prod_config: URIAllowlistConfig) -> None:
        result = validate_uri("file:///workspace/data/train.csv", prod_config)
        assert result.is_valid is True
        assert result.scheme == URIScheme.FILE
        assert result.resolved_path is not None

    def test_etc_passwd_rejected(self, prod_config: URIAllowlistConfig) -> None:
        result = validate_uri("file:///etc/passwd", prod_config)
        assert result.is_valid is False
        assert result.rejection_reason == "system_path"

    def test_etc_shadow_rejected(self, prod_config: URIAllowlistConfig) -> None:
        result = validate_uri("file:///etc/shadow", prod_config)
        assert result.is_valid is False
        assert result.rejection_reason == "system_path"

    def test_proc_rejected(self, prod_config: URIAllowlistConfig) -> None:
        result = validate_uri("file:///proc/1/environ", prod_config)
        assert result.is_valid is False
        assert result.rejection_reason == "system_path"

    def test_path_traversal_rejected(self, prod_config: URIAllowlistConfig) -> None:
        result = validate_uri("file:///workspace/data/../../etc/passwd", prod_config)
        assert result.is_valid is False
        assert result.rejection_reason == "path_traversal"

    def test_outside_volume_roots_rejected(self, prod_config: URIAllowlistConfig) -> None:
        result = validate_uri("file:///tmp/secret", prod_config)
        assert result.is_valid is False
        assert result.rejection_reason == "outside_volume_roots"

    def test_empty_path_rejected(self, prod_config: URIAllowlistConfig) -> None:
        result = validate_uri("file://", prod_config)
        assert result.is_valid is False
        assert result.rejection_reason == "empty_path"


# ---------------------------------------------------------------------------
# validate_uri - http/https
# ---------------------------------------------------------------------------


class TestValidateHttpUri:
    def test_approved_https_host_accepted(self, prod_config: URIAllowlistConfig) -> None:
        result = validate_uri("https://s3.amazonaws.com/bucket/key", prod_config)
        assert result.is_valid is True
        assert result.host == "s3.amazonaws.com"

    def test_localhost_rejected_in_production(self, prod_config: URIAllowlistConfig) -> None:
        result = validate_uri("http://127.0.0.1/admin", prod_config)
        assert result.is_valid is False
        assert result.rejection_reason == "localhost_forbidden"

    def test_localhost_name_rejected(self, prod_config: URIAllowlistConfig) -> None:
        result = validate_uri("http://localhost/admin", prod_config)
        assert result.is_valid is False
        assert result.rejection_reason == "localhost_forbidden"

    def test_ipv6_loopback_rejected(self, prod_config: URIAllowlistConfig) -> None:
        result = validate_uri("http://[::1]/admin", prod_config)
        assert result.is_valid is False
        assert result.rejection_reason == "localhost_forbidden"

    def test_private_ip_10_rejected(self, prod_config: URIAllowlistConfig) -> None:
        result = validate_uri("http://10.0.0.1/x", prod_config)
        assert result.is_valid is False
        assert result.rejection_reason == "private_ip_forbidden"

    def test_private_ip_172_rejected(self, prod_config: URIAllowlistConfig) -> None:
        result = validate_uri("http://172.16.0.1/x", prod_config)
        assert result.is_valid is False
        assert result.rejection_reason == "private_ip_forbidden"

    def test_private_ip_192_rejected(self, prod_config: URIAllowlistConfig) -> None:
        result = validate_uri("http://192.168.1.1/x", prod_config)
        assert result.is_valid is False
        assert result.rejection_reason == "private_ip_forbidden"

    def test_link_local_169_rejected(self, prod_config: URIAllowlistConfig) -> None:
        result = validate_uri("http://169.254.169.254/latest/meta-data", prod_config)
        assert result.is_valid is False
        assert result.rejection_reason == "private_ip_forbidden"

    def test_arbitrary_host_rejected(self, prod_config: URIAllowlistConfig) -> None:
        result = validate_uri("https://evil.example.com/x", prod_config)
        assert result.is_valid is False
        assert result.rejection_reason == "host_not_allowed"

    def test_dev_mode_allows_localhost(self, dev_config: URIAllowlistConfig) -> None:
        result = validate_uri("http://127.0.0.1/local", dev_config)
        assert result.is_valid is True

    def test_dev_mode_allows_arbitrary_http(self, dev_config: URIAllowlistConfig) -> None:
        result = validate_uri("https://evil.example.com/x", dev_config)
        assert result.is_valid is True

    def test_missing_host_rejected(self, prod_config: URIAllowlistConfig) -> None:
        result = validate_uri("https:///path", prod_config)
        assert result.is_valid is False
        assert result.rejection_reason == "missing_host"


# ---------------------------------------------------------------------------
# validate_uri - s3/gs/azblob
# ---------------------------------------------------------------------------


class TestValidateObjectUri:
    def test_s3_approved_bucket(self, prod_config: URIAllowlistConfig) -> None:
        result = validate_uri("s3://my-bucket.s3.amazonaws.com/key", prod_config)
        assert result.is_valid is True
        assert result.scheme == URIScheme.S3

    def test_s3_disallowed_bucket(self, prod_config: URIAllowlistConfig) -> None:
        result = validate_uri("s3://evil-bucket/key", prod_config)
        assert result.is_valid is False
        assert result.rejection_reason == "bucket_not_allowed"

    def test_gs_approved(self, prod_config: URIAllowlistConfig) -> None:
        result = validate_uri("gs://my-bucket.storage.googleapis.com/key", prod_config)
        assert result.is_valid is True
        assert result.scheme == URIScheme.GS

    def test_gs_disallowed(self, prod_config: URIAllowlistConfig) -> None:
        result = validate_uri("gs://evil-bucket/key", prod_config)
        assert result.is_valid is False

    def test_azblob_approved(self, prod_config: URIAllowlistConfig) -> None:
        result = validate_uri("azblob://myaccount.blob.core.windows.net/container/key", prod_config)
        assert result.is_valid is True
        assert result.scheme == URIScheme.AZBLOB

    def test_azblob_disallowed(self, prod_config: URIAllowlistConfig) -> None:
        result = validate_uri("azblob://evilaccount/key", prod_config)
        assert result.is_valid is False

    def test_s3_with_credentials_still_validated(self, prod_config: URIAllowlistConfig) -> None:
        result = validate_uri("s3://AKIA:SECRET@my-bucket.s3.amazonaws.com/key", prod_config)
        assert result.is_valid is True


# ---------------------------------------------------------------------------
# validate_uri - runpod_volume
# ---------------------------------------------------------------------------


class TestValidateRunpodVolumeUri:
    def test_approved_root(self, prod_config: URIAllowlistConfig) -> None:
        result = validate_uri("runpod_volume:///workspace/data/x.parquet", prod_config)
        assert result.is_valid is True
        assert result.scheme == URIScheme.RUNPOD_VOLUME

    def test_outside_root_rejected(self, prod_config: URIAllowlistConfig) -> None:
        result = validate_uri("runpod_volume:///tmp/x", prod_config)
        assert result.is_valid is False
        assert result.rejection_reason == "outside_volume_roots"

    def test_traversal_rejected(self, prod_config: URIAllowlistConfig) -> None:
        result = validate_uri("runpod_volume:///workspace/data/../../etc", prod_config)
        assert result.is_valid is False
        assert result.rejection_reason == "path_traversal"


# ---------------------------------------------------------------------------
# validate_uri - fail-closed / edge cases
# ---------------------------------------------------------------------------


class TestValidateUriEdgeCases:
    def test_empty_uri_rejected(self, prod_config: URIAllowlistConfig) -> None:
        result = validate_uri("", prod_config)
        assert result.is_valid is False
        assert result.rejection_reason == "empty_uri"

    def test_whitespace_uri_rejected(self, prod_config: URIAllowlistConfig) -> None:
        result = validate_uri("   ", prod_config)
        assert result.is_valid is False
        assert result.rejection_reason == "empty_uri"

    def test_missing_scheme_rejected(self, prod_config: URIAllowlistConfig) -> None:
        result = validate_uri("/workspace/data/x.csv", prod_config)
        assert result.is_valid is False
        assert result.rejection_reason == "missing_scheme"

    def test_unknown_scheme_rejected(self, prod_config: URIAllowlistConfig) -> None:
        result = validate_uri("ftp://host/file", prod_config)
        assert result.is_valid is False
        assert result.rejection_reason == "unknown_scheme"

    def test_scheme_not_in_allowed_list(self) -> None:
        cfg = URIAllowlistConfig(allowed_schemes=[URIScheme.FILE])
        result = validate_uri("https://s3.amazonaws.com/x", cfg)
        assert result.is_valid is False
        assert result.rejection_reason == "scheme_not_allowed"

    def test_very_long_path(self, prod_config: URIAllowlistConfig) -> None:
        long_segment = "a" * 1000
        uri = f"file:///workspace/data/{long_segment}.csv"
        result = validate_uri(uri, prod_config)
        assert result.is_valid is True

    def test_malformed_uri_does_not_raise(self, prod_config: URIAllowlistConfig) -> None:
        # ``urlsplit`` is lenient; this should not raise even if odd.
        result = validate_uri("file://[::1", prod_config)
        assert isinstance(result, URIValidationResult)


# ---------------------------------------------------------------------------
# validate_uris (batch)
# ---------------------------------------------------------------------------


class TestValidateUris:
    def test_batch_preserves_order(self, prod_config: URIAllowlistConfig) -> None:
        uris = [
            "file:///workspace/data/x.csv",
            "file:///etc/passwd",
            "https://s3.amazonaws.com/k",
        ]
        results = validate_uris(uris, prod_config)
        assert len(results) == 3
        assert results[0].is_valid is True
        assert results[1].is_valid is False
        assert results[2].is_valid is True

    def test_empty_batch(self, prod_config: URIAllowlistConfig) -> None:
        assert validate_uris([], prod_config) == []

    def test_all_rejected(self, prod_config: URIAllowlistConfig) -> None:
        uris = ["", "ftp://x", "file:///etc/passwd"]
        results = validate_uris(uris, prod_config)
        assert all(r.is_valid is False for r in results)
