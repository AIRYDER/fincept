"""Receipt-integrity guard for RunPod import-bisection receipt bundles.

Background: Import Bisection Test F (c0f15fa7) produced a false-negative
summary for the ``lightgbm`` profile. The summary declared
``worker_died_while_job_in_queue`` but the raw probe JSONL showed the worker
was ``running=1, unhealthy=0`` at the last poll — the worker was actively
processing the job, not dead. The bisection probe logic has since been fixed
(item 9 in docs/runpod-fix-plan/07-remaining-work.md), but this test ensures
future receipt bundles cannot silently contradict their raw evidence.

The guard scans every receipt bundle under
``reports/runpod-test-runs/*/import-bisection/`` that has both a
``summary.json`` and per-profile ``probe-*.jsonl`` / ``status-final-*.json``
files. It fails when:

  * ``summary.json`` says a profile ``result`` is ``pass`` but the
    corresponding ``status-final-<profile>.json`` shows a non-COMPLETED
    terminal status.
  * ``summary.json`` says a profile ``result`` is ``fail`` but the
    corresponding ``status-final-<profile>.json`` shows COMPLETED.
  * ``summary.json`` says ``worker_died_while_job_in_queue`` but the last
    ``poll`` event in ``probe-<profile>.jsonl`` shows ``running >= 1`` and
    ``unhealthy == 0`` (worker was actively processing, not dead).

The test does NOT hardcode any specific SHA or profile values — it derives
expectations from the raw evidence in each bundle.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# Root containing all runpod-test-runs receipt bundles.
REPORTS_ROOT = Path(__file__).resolve().parents[2] / "reports" / "runpod-test-runs"


def _find_bisection_bundles() -> list[Path]:
    """Find all import-bisection receipt bundles with a summary.json."""
    if not REPORTS_ROOT.is_dir():
        return []
    bundles = []
    for sha_dir in REPORTS_ROOT.iterdir():
        bisect_dir = sha_dir / "import-bisection"
        summary = bisect_dir / "summary.json"
        if summary.is_file():
            bundles.append(bisect_dir)
    return sorted(bundles)


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_jsonl(path: Path) -> list[dict]:
    lines = [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    return [json.loads(ln) for ln in lines]


def _last_poll(probe_events: list[dict]) -> dict | None:
    """Return the last 'poll' event from a probe JSONL, or None."""
    polls = [e for e in probe_events if e.get("event") == "poll"]
    return polls[-1] if polls else None


# Parametrize over every discovered bundle so each is reported independently.
BUNDLES = _find_bisection_bundles()


@pytest.mark.parametrize(
    "bundle",
    BUNDLES,
    ids=[b.parent.name for b in BUNDLES],
)
def test_summary_matches_terminal_status(bundle: Path) -> None:
    """summary.json result must match status-final-<profile>.json."""
    summary = _load_json(bundle / "summary.json")
    for entry in summary.get("results", []):
        profile = entry["profile"]
        result = entry["result"]
        status_file = bundle / f"status-final-{profile}.json"
        if not status_file.is_file():
            # No raw status evidence — cannot verify. Skip this profile.
            continue
        status_data = _load_json(status_file)
        final_status = status_data.get("final_status", "")
        if result == "pass":
            assert final_status == "COMPLETED", (
                f"{profile}: summary says 'pass' but status-final is "
                f"'{final_status}', not COMPLETED"
            )
        elif result == "fail":
            assert final_status != "COMPLETED", (
                f"{profile}: summary says 'fail' but status-final is "
                f"'COMPLETED' — raw evidence contradicts the summary"
            )


@pytest.mark.parametrize(
    "bundle",
    BUNDLES,
    ids=[b.parent.name for b in BUNDLES],
)
def test_worker_died_claim_matches_probe_evidence(bundle: Path) -> None:
    """If summary says 'worker_died_while_job_in_queue', the last probe poll
    must show no active worker (running=0, unhealthy=0).

    A worker with running=1 and unhealthy=0 is actively processing, not dead.
    """
    summary = _load_json(bundle / "summary.json")
    for entry in summary.get("results", []):
        profile = entry["profile"]
        reason = entry.get("failure_reason", "")
        if reason != "worker_died_while_job_in_queue":
            continue
        probe_file = bundle / f"probe-{profile}.jsonl"
        if not probe_file.is_file():
            continue
        probe_events = _load_jsonl(probe_file)
        last_poll = _last_poll(probe_events)
        if last_poll is None:
            continue
        workers = last_poll.get("health", {}).get("workers", {})
        running = workers.get("running", 0)
        unhealthy = workers.get("unhealthy", 0)
        assert not (running >= 1 and unhealthy == 0), (
            f"{profile}: summary claims 'worker_died_while_job_in_queue' but "
            f"last probe poll shows running={running}, unhealthy={unhealthy} "
            f"— worker was actively processing, not dead"
        )


@pytest.mark.parametrize(
    "bundle",
    BUNDLES,
    ids=[b.parent.name for b in BUNDLES],
)
def test_summary_profiles_have_probe_evidence(bundle: Path) -> None:
    """Every profile in summary.json must have a probe-<profile>.jsonl file."""
    summary = _load_json(bundle / "summary.json")
    for entry in summary.get("results", []):
        profile = entry["profile"]
        probe_file = bundle / f"probe-{profile}.jsonl"
        assert probe_file.is_file(), (
            f"{profile}: summary references this profile but "
            f"probe-{profile}.jsonl is missing"
        )


def test_bundles_exist() -> None:
    """At least one bisection bundle should exist for testing."""
    if not BUNDLES:
        pytest.skip("No import-bisection receipt bundles found")
