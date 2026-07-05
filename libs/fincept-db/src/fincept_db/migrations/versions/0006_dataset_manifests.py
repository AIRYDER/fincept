"""dataset manifests table

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-05 00:00:00.000000

Persists the dataset manifest registry — the durable, append-only home for
point-in-time dataset manifests. One table (``dataset_manifests``) mirrors the
``FeatureLakeManifest`` fields that affect reproducibility so a training worker
references a manifest hash instead of DB credentials.

Design rules (see references/fincept-db-schema.md):
  - JSONB for structured fields (``purged_fold_spec``), BigInteger for ns
    timestamps and embargo length.
  - CHECK constraints for enum-like columns (``readiness_level``) and
    non-negative invariants (``row_count >= 0``) so the DB rejects bad values
    even if Python is bypassed.
  - No secrets, no DB credentials, no raw payloads in any column. The manifest
    hash covers every field that affects a training run (schema hashes,
    universe, row count, checksum, folds, PIT flag) so two exports of the same
    PIT dataset produce the same hash and a single changed row changes the hash.
  - ``pit_proof_verified`` is a boolean that is only True after the builder has
    asserted every feature value's ``observed_at <= decision_time``.
  - ``readiness_level`` gates production dispatch (L3+ required).
  - Unique constraint on ``(dataset_id, manifest_hash)`` so the same dataset
    re-registered with changed data produces a new row (versioning by hash).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # --- dataset_manifests (point-in-time dataset manifest registry) ---
    op.create_table(
        "dataset_manifests",
        sa.Column("manifest_id", sa.String(128), primary_key=True, nullable=False),
        sa.Column("dataset_id", sa.String(128), nullable=False),
        sa.Column("manifest_hash", sa.String(64), nullable=False),
        sa.Column("manifest_uri", sa.String(1024), nullable=True),
        sa.Column("data_uri", sa.String(1024), nullable=True),
        sa.Column("data_sha256", sa.String(64), nullable=True),
        sa.Column("data_format", sa.String(16), nullable=True),
        sa.Column("row_count", sa.Integer, nullable=False),
        sa.Column("feature_schema_hash", sa.String(64), nullable=False),
        sa.Column("label_schema_hash", sa.String(64), nullable=False),
        sa.Column(
            "readiness_level",
            sa.String(16),
            nullable=False,
            server_default="'L1'",
        ),
        sa.Column("pit_proof_verified", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column(
            "purged_fold_spec",
            JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("embargo_length", sa.BigInteger, nullable=True),
        sa.Column("quality_report_uri", sa.String(1024), nullable=True),
        sa.Column("quality_report_sha256", sa.String(64), nullable=True),
        sa.Column("created_at_ns", sa.BigInteger, nullable=False),
        sa.Column("updated_at_ns", sa.BigInteger, nullable=False),
        sa.CheckConstraint(
            "readiness_level IN ('L1','L2','L3','L4')",
            name="ck_dataset_manifests_readiness_level_domain",
        ),
        sa.CheckConstraint(
            "row_count >= 0", name="ck_dataset_manifests_row_count_nonneg"
        ),
        sa.UniqueConstraint(
            "dataset_id",
            "manifest_hash",
            name="uq_dataset_manifests_dataset_id_manifest_hash",
        ),
    )
    op.create_index(
        "ix_dataset_manifests_dataset_id", "dataset_manifests", ["dataset_id"]
    )
    op.create_index(
        "ix_dataset_manifests_readiness_level",
        "dataset_manifests",
        ["readiness_level"],
    )
    op.create_index(
        "ix_dataset_manifests_manifest_hash", "dataset_manifests", ["manifest_hash"]
    )


def downgrade() -> None:
    op.drop_index(
        "ix_dataset_manifests_manifest_hash", table_name="dataset_manifests"
    )
    op.drop_index(
        "ix_dataset_manifests_readiness_level", table_name="dataset_manifests"
    )
    op.drop_index(
        "ix_dataset_manifests_dataset_id", table_name="dataset_manifests"
    )
    op.drop_table("dataset_manifests")
