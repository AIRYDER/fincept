---
title: "Jane Street Capital — Open-Source Repos (F# for Trading)"
authors: ["Jane Street Capital"]
affiliation: "Jane Street Capital"
source: "https://github.com/janestreet"
date: "2018-01-01"
added: "2026-06-22"
last_reviewed: "2026-06-22"
status: "verified"
relevance: "medium"
tier_mapping: ["none"]
tags: ["jane-street", "fsharp", "trading", "infrastructure", "open-source"]
license: "MIT"  # mostly; per-repo check needed
effort_to_apply: "L"
adoption_risk: "low"
---

## TL;DR
Jane Street, one of the largest market-making firms, open-sources a substantial portion of their internal infrastructure in OCaml (their primary trading language) and F# (for .NET interop). Key open-source repos include: `magic-trace` (tracing), `core` (utility library), `incr_dom` (incremental DOM for web UIs), `ppx_sexp_conv` (s-expression converters), and the `F# Template` (financial types). The repos are well-documented, well-tested, and represent best-in-class engineering for low-latency trading systems.

## Why we care
Fincept is a Python-based system, not OCaml/F#. Direct adoption of Jane Street's code is impractical. But the *patterns* are valuable: the `core` library's design for `Time_ns` (monotonic clock), the `Incrmap` (incremental map), the `Incremental` library (incremental computation for real-time UIs). These patterns can be studied and ported to Python (e.g., `sortedcontainers`, `networkx`).

## Key ideas
- F# for trading: F# is a functional-first language on .NET. Jane Street uses F# for the systems that interact with Windows-only APIs (e.g., exchange gateways).
- `Time_ns` and `Time.Span`: nanosecond-precision time types. Standard for HFT.
- Incremental computation: instead of recomputing everything on every update, maintain a DAG of computations and re-evaluate only the affected nodes. Jane Street's `incr_dom` and `incremental` libraries are best-in-class.
- S-expressions for serialization: `ppx_sexp_conv` and `sexplib` are the OCaml equivalent of JSON/Pickle but with stronger type guarantees.

## How to apply to Fincept
1. NOT recommended for direct adoption. Use as a reference.
2. The pattern of "monotonic clock + typed time" maps to `libs/fincept-core/src/fincept_core/clock.py` in Fincept.
3. The incremental-computation pattern maps to the Fincept dashboard's React components (which use `useMemo` and `useCallback` for incremental computation).

## Caveats
- Direct adoption is impractical (different language stack).
- Some of Jane Street's code is proprietary; the open-source repos are not the full picture.
- The patterns are best-in-class for low-latency trading; Fincept is paper-trading, so the patterns are overkill.

## Related entries
- `research/repos/hudson-river-hftbacktest.md` (Hudson River's open-source)
- `research/architectures/two-sigma-research-platform.md` (Two Sigma's architecture)

## References
- https://github.com/janestreet
- Yaron Minsky's talks: https://blogs.janestreet.com/ (search "OCaml" and "incremental")
- "F# for Trading" talk by Don Syme (Jane Street, 2019)
