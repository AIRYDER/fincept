"""quant_foundry.callback_metrics - durable callback rejection-rate store.

Why this module exists
~~~~~~~~~~~~~~~~~~~~~~

``shadow_health`` used to return ``callback_rejection_rate = None`` because
the gateway rejected bad HMAC signatures without leaving any durable trace
(see ``receive_callback`` — a bad signature is fail-closed with no inbox
record). That made the rejection rate unobservable from durable state, so
operators could not tell whether callbacks were silently being dropped.

``CallbackMetricsStore`` writes one append-only JSONL line per callback
event (``received`` / ``accepted`` / ``rejected``) at
``data/quant_foundry/callback_metrics.jsonl`` so ``shadow_health`` can
compute a rolling ``rejected / (accepted + rejected)`` over a configurable
window without depending on the inbox (which by design does not record
bad-signature events).

Filesystem layout
~~~~~~~~~~~~~~~~~

  data/quant_foundry/callback_metrics.jsonl

One file, append-only. Each line is a JSON object shaped::

  {"ts_ns": int, "event": "received" | "accepted" | "rejected",
   "reason_code": str | None}

The record intentionally carries NO secret and NO raw payload — only a
timestamp, an event label, and an optional reason code. This keeps the
metrics file safe to ``cat`` / ship to a dashboard.

Why filesystem instead of Postgres / Redis?
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Same reasoning as ``fincept_core.prediction_log``: zero infra, ``cat``-able
state, easy backup, survives ``FLUSHDB``. The volume is tiny (one line per
callback) and the reader only ever tails the last window.
"""

from __future__ import annotations

import json
import os
import pathlib
import time
from typing import Any

# --------------------------------------------------------------------------- #
# Configuration                                                              #
# --------------------------------------------------------------------------- #


def _default_metrics_dir() -> pathlib.Path:
    return pathlib.Path(
        os.environ.get(
            "QUANT_FOUNDRY_CALLBACK_METRICS_DIR",
            "data/quant_foundry",
        )
    )


# Valid event labels. ``received`` is recorded on every inbound callback
# (before verification); ``accepted`` / ``rejected`` after the decision.
_VALID_EVENTS = frozenset({"received", "accepted", "rejected"})


# --------------------------------------------------------------------------- #
# The store                                                                  #
# --------------------------------------------------------------------------- #


class CallbackMetricsStore:
    """Append-only callback-metrics record on the filesystem.

    Constructor takes the metrics root directory. Tests pass a ``tmp_path``;
    production reads from ``$QUANT_FOUNDRY_CALLBACK_METRICS_DIR`` (default
    ``data/quant_foundry``). The JSONL file is
    ``<root>/callback_metrics.jsonl``.

    The store is intentionally minimal: it appends events and computes a
    rolling rejection rate. It does NOT deduplicate, rotate, or index —
    matching the simplicity of ``fincept_core.prediction_log`` until
    profiling demands otherwise.
    """

    def __init__(self, *, metrics_dir: pathlib.Path | None = None) -> None:
        self._metrics_dir = metrics_dir or _default_metrics_dir()

    @property
    def metrics_dir(self) -> pathlib.Path:
        return self._metrics_dir

    def _path(self) -> pathlib.Path:
        return self._metrics_dir / "callback_metrics.jsonl"

    # ------------------------------------------------------------------ #
    # Write                                                              #
    # ------------------------------------------------------------------ #

    def record(
        self,
        event: str,
        *,
        reason_code: str | None = None,
        ts_ns: int | None = None,
    ) -> None:
        """Append a single metric event.

        ``event`` must be one of ``received`` / ``accepted`` / ``rejected``.
        ``reason_code`` is an optional short label (e.g.
        ``"bad_signature"``, ``"missing_runpod_callback_fields"``) — never
        a secret or raw payload.

        On write failure we raise rather than silently drop the event
        (the plan forbids silent drops). Callers that want best-effort
        recording can catch ``OSError``.
        """
        if event not in _VALID_EVENTS:
            raise ValueError(f"event must be one of {sorted(_VALID_EVENTS)}; got {event!r}")
        if reason_code is not None and not isinstance(reason_code, str):
            raise TypeError("reason_code must be a str or None")

        record_ts_ns = int(ts_ns) if ts_ns is not None else time.time_ns()
        line_obj: dict[str, Any] = {
            "ts_ns": record_ts_ns,
            "event": event,
            "reason_code": reason_code,
        }
        line = json.dumps(line_obj, separators=(",", ":"))

        self._metrics_dir.mkdir(parents=True, exist_ok=True)
        path = self._path()
        # One write call per line. On POSIX a write smaller than
        # PIPE_BUF (4096) is atomic; our records are ~80 bytes, well
        # under that ceiling. On NTFS a single-cluster write is atomic
        # for our purposes. We do NOT use temp-file-then-rename because
        # rename would replace the whole log rather than append a line.
        with path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    # ------------------------------------------------------------------ #
    # Read                                                               #
    # ------------------------------------------------------------------ #

    def _iter_events(self) -> list[dict[str, Any]]:
        """Read and parse all events from the JSONL file.

        Malformed lines (bad JSON, missing keys, wrong types) are skipped
        rather than raising — a single corrupt line must not take the
        whole read down. This matches the tolerance pattern in
        ``fincept_core.prediction_log`` and ``api.promotions``.
        """
        path = self._path()
        if not path.is_file():
            return []

        events: list[dict[str, Any]] = []
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    ts_ns = int(obj["ts_ns"])
                    event = str(obj["event"])
                    reason_code = obj.get("reason_code")
                    if reason_code is not None:
                        reason_code = str(reason_code)
                except (json.JSONDecodeError, KeyError, ValueError, TypeError):
                    # Skip malformed lines — never crash the read.
                    continue
                if event not in _VALID_EVENTS:
                    continue
                events.append(
                    {
                        "ts_ns": ts_ns,
                        "event": event,
                        "reason_code": reason_code,
                    }
                )
        return events

    # ------------------------------------------------------------------ #
    # Aggregate                                                          #
    # ------------------------------------------------------------------ #

    def rejection_rate(
        self,
        window_ns: int = 24 * 3600 * 1_000_000_000,
    ) -> float:
        """Return ``rejected / (accepted + rejected)`` over the last window.

        ``window_ns`` is a nanosecond look-back from ``time.time_ns()``;
        only events with ``ts_ns >= now - window_ns`` are counted. The
        ``received`` event is intentionally excluded from the denominator
        — a callback that was received but not yet adjudicated should not
        dilute the rejection rate.

        Returns ``0.0`` (NOT an exception) when:
          * the store is empty / has no in-window events, or
          * there are no accepted + rejected events in the window
            (divide-by-zero).
        """
        if window_ns < 0:
            raise ValueError("window_ns must be non-negative")

        events = self._iter_events()
        if not events:
            return 0.0

        now_ns = time.time_ns()
        cutoff_ns = now_ns - window_ns
        accepted = 0
        rejected = 0
        for ev in events:
            if ev["ts_ns"] < cutoff_ns:
                continue
            if ev["event"] == "accepted":
                accepted += 1
            elif ev["event"] == "rejected":
                rejected += 1

        denominator = accepted + rejected
        if denominator == 0:
            return 0.0
        return rejected / denominator

    def has_any_events(self) -> bool:
        """Return True if at least one event has ever been recorded.

        Used by ``shadow_health`` to decide whether to surface ``None``
        (no callbacks recorded yet) or a numeric rate.
        """
        return bool(self._iter_events())
