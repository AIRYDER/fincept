# Maintenance Checklist — Fincept BridgeMemory hub

Periodic hygiene for the hub at `C:\Users\nolan\CascadeProjects\fincept-terminal\.bridgememory\`. Run when MCP tools are available.

## Daily / per-task

- [ ] After any decision: capture the *because*, not just the *what*. `append_to_memory` to the most relevant existing memory, or `create_memory` if it's a new topic.
- [ ] After closing a finding: update `[[Outstanding Findings Table]]` — remove the row, add a closure note under the relevant audit memory (`Closed in commit <hash>: ...`).
- [ ] After adding a new code module: search for related memories first, then create one only if the topic is genuinely new. Always wikilink to peers.

## Weekly / per-phase

- [ ] `list_orphans` — check for memories with no incoming or outgoing wikilinks. Either connect them or accept them as intentional Zettelkasten state. **Do not delete on your own initiative.**
- [ ] `find_backlinks` on the hub root memory — confirm the major entry points are still pointed at correctly.
- [ ] Spot-check `[[Title]]` references resolve to a real H1. If a wikilink target was renamed in the UI, update the wikilinks.

## When MCP recovers from fallback

- [ ] `list_memories` — confirm the `.md` files written during fallback are indexed.
- [ ] `read_memory` one of them — confirm the H1 title matches what other memories link to (or fix the wikilinks).
- [ ] No migration needed — the files are already on disk and renderer-compatible.

## When a new evidence file lands in `.omo/evidence/`

- [ ] Read or skim the file.
- [ ] Decide its slot (top-level / audit / task / finding).
- [ ] `create_memory` with the appropriate slot title.
- [ ] Update the relevant index memory (`Fincept Evidence Hub`, `Outstanding Findings Table`, etc.).
- [ ] Never delete or rewrite the source file.

## Anti-patterns to flag

- [ ] Memories longer than ~300 lines — split.
- [ ] Memories covering multiple distinct ideas — split.
- [ ] Bodies without outgoing `[[wikilinks]]` — connect them.
- [ ] Bodies without `## Related` section — add one.
- [ ] Duplicate titles — resolve or merge.
- [ ] Inline `#tags` — not queryable; remove and convert to wikilinks.

## Quarterly health check

- [ ] Total memory count trend (should grow steadily, not spiky).
- [ ] Orphan ratio (target: < 20%).
- [ ] Average wikilinks per memory (target: ≥ 3).
- [ ] Oldest unlinked memories — review for relevance; close or connect.

## Related

- `../SKILL.md`
- `hub-map.md`
- `fallback.md`