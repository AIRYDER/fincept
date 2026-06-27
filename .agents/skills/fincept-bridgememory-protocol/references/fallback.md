# Fallback Protocol — BridgeMemory MCP unavailable

When `mcp__bridgememory__*` tools return errors, **do not give up**. The hub is on disk; you can read and write it directly.

## When to invoke

Activate this fallback when:

- `mcp__bridgememory__hub_status` returns `{"error": ... "token mismatch" ...}`
- Any `mcp__bridgememory__*` tool returns a runtime/unavailable error
- BridgeMemory tools are missing from the tool list entirely

Do NOT invoke it just because the user asked a quick question — first try MCP; only fall back when MCP demonstrably fails.

## Read path (no MCP)

```text
1. List memories:    ls C:\Users\nolan\CascadeProjects\fincept-terminal\.bridgememory\*.md
2. Read one:         Read C:\Users\nolan\CascadeProjects\fincept-terminal\.bridgememory\<File>.md
3. Search:           Grep pattern="<token>" path="C:\Users\nolan\CascadeProjects\fincept-terminal\.bridgememory" glob="*.md"
4. Find backlinks:   Grep pattern="\[\[<Title>\]\]" path="...\.bridgememory" glob="*.md"
```

`[[Title]]` resolution is case-insensitive on the H1 (first `# Title` line) of each file.

## Write path (no MCP)

### Create new memory

```text
Write(
  file_path: "C:\Users\nolan\CascadeProjects\fincept-terminal\.bridgememory\<Title>.md",
  content: "# Title\n\nLead claim.\n\nEvidence.\n\n## Related\n- [[Peer Memory]]\n- [[Another Memory]]\n"
)
```

### Append to existing memory

```text
Edit(
  file_path: "<existing .md>",
  old_string: "## Related\n- [[Existing Link]]\n",
  new_string: "## Related\n- [[Existing Link]]\n- [[Newly Linked Memory]]\n"
)
```

Sequence appends — do NOT parallelize Edit on the same file.

### Update entire body

```text
1. Read the current file
2. Compose the new body in memory
3. Write the whole file back
```

Use this only when the body is materially wrong/restructured. Prefer Edit (append) otherwise.

## Hard rules during fallback

- **Never edit** `index.json` or `welcome.md` — those are hub metadata.
- **Never delete** `.md` files unless the builder explicitly says delete.
- **Filename slugification is auto on MCP create.** When writing directly, mirror the H1 title with spaces → em-dash and spaces preserved (`Title With Words.md`). The renderer is forgiving; titles resolve by H1, not filename.
- **Wikilinks must be `[[Title]]`** with the exact H1 of the target, case-insensitive. If you're unsure, list existing memories first and confirm the title.

## Recovery

When MCP comes back online:

- All `.md` files you wrote during fallback are already there — no migration needed.
- The renderer picks them up on next BridgeSpace open.
- Wikilinks resolve correctly because they target H1 titles, not filenames.
- The MCP `list_memories` will see them immediately (or on next refresh — check by reading one).

## Why this matters

The builder invested in the hub. Skipping memory because the MCP is offline defeats the investment. The fallback keeps the loop alive.

## Related

- `../SKILL.md` — main skill file
- `hub-map.md` — current inventory
- `maintenance-checklist.md` — periodic hygiene