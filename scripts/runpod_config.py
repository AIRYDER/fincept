"""Shared RunPod resource IDs — single source of truth.

All scripts that reference RunPod template IDs, endpoint IDs, or network
volume IDs should import from this module instead of hardcoding the values.

Override via environment variables for non-production environments:
  RUNPOD_TRAINING_TEMPLATE_ID
  RUNPOD_INFERENCE_TEMPLATE_ID
  RUNPOD_TRAINING_ENDPOINT_ID
  RUNPOD_INFERENCE_ENDPOINT_ID
  RUNPOD_NETWORK_VOLUME_ID
"""

from __future__ import annotations

import os

# --- Template IDs ----------------------------------------------------------
TRAINING_TEMPLATE_ID = os.environ.get(
    "RUNPOD_TRAINING_TEMPLATE_ID", "me58r5vdrp"
)
INFERENCE_TEMPLATE_ID = os.environ.get(
    "RUNPOD_INFERENCE_TEMPLATE_ID", "wnasp3v5jn"
)

# --- Endpoint IDs ----------------------------------------------------------
TRAINING_ENDPOINT_ID = os.environ.get(
    "RUNPOD_TRAINING_ENDPOINT_ID", "h2blqodcicxqyy"
)
INFERENCE_ENDPOINT_ID = os.environ.get(
    "RUNPOD_INFERENCE_ENDPOINT_ID", "t31u1z426jy1ub"
)

# --- Network Volume ID -----------------------------------------------------
NETWORK_VOLUME_ID = os.environ.get(
    "RUNPOD_NETWORK_VOLUME_ID", "rrsd005i3g"
)

# --- Endpoint names (human-readable, used in API queries) ------------------
TRAINING_NAME = "fincept-qf-training"
INFERENCE_NAME = "fincept-qf-inference"

# --- GPU type --------------------------------------------------------------
GPU_TYPE = "NVIDIA GeForce RTX 4090"
