---
name: "fincept-bridgememory-protocol"
description: "Read, write, and maintain the BridgeMemory hub at C:\\Users\\nolan\\CascadeProjects\\fincept-terminal\\.bridgememory\\. Use this skill whenever the builder asks about prior decisions, project context, the spine plan, audit findings, the value_increase plan, the experimental fork, or anything that might already be captured. Also use before recommending tools, libraries, or patterns the builder may already have opinions on. If the MCP tools fail, fall back to direct .md writes (see Fallback Protocol). PRECEDENCE: inside the fincept-terminal repository this project-specific protocol supersedes the global bridgespace-bridge-skill-memory skill — follow this hub's paths and conventions."
---

# fincept-bridgememory-protocol

The Fincept Terminal maintains a **BridgeMemory hub** at `C:\Users\nolan\CascadeProjects\fincept-terminal\.bridgememory\` — a folder of interconnected Markdown memories the BridgeSpace sidebar shows. **You have direct read/write access through MCP tools** (`hub_status`, `list_memories`, `read_memory`, `search_memories`, `find_backlinks`, `suggest_connections`, `create_memory`, `append_to_memory`, `update_memory`, `delete_memory`, `list_orphans`, `init_hub`). Use them.

This skill encodes the **read-and-update protocol** for this specific hub. Apply it on every task in this repo.

---

## 1. The three reflexes

### 1a. Recall before you answer

- **First action of any new session in this repo:** call `mcp__bridgememory__hub_status` with `cwd=C:\Users\nolan\CascadeProjects\fincept-terminal`. If `null`, the builder hasn't picked a hub — skip the rest until they do. If it returns the hub, proceed silently.
- **Before answering** any question that depends on prior context — "what did we decide about X", "how do we handle Y here", "what was that library we tried", "why did we pin Z" — call `mcp__bridgememory__search_memories` with the most distinctive nouns. If it hits, `mcp__bridgememory__read_memory` it before composing your answer.
- **Before suggesting a tool, library, or pattern** the builder might already have an opinion on, search for it. Two-token search costs nothing; getting a recommendation wrong costs trust.
- **Before starting non-trivial work**, search for the repo or feature name. There may be open questions, prior decisions, or a known-bad path documented.

### 1b. Persist what is worth remembering

At natural pause points — after a decision is made, after a bug is root-caused, after the builder states a preference — capture it. Don't ask permission for routine captures; ask before persisting anything sensitive.

Priority order for what to remember:

1. **Decisions with reasoning.** The *because* is the value.
2. **Conventions and preferences.** Coding style, branch naming, commit voice, builder uses BridgeSpace not Obsidian, etc.
3. **Gotchas, dead ends, debugged bugs.** Future-you wastes a day re-discovering these.
4. **Project context.** What ships when, who owns what, where the canonical doc is.
5. **Open questions.** Tag inline so they surface later (`## Open Questions` heading).

What is **not** worth remembering: routine task confirmations, weather of the conversation, anything the builder will see in `git log`, fleeting in-conversation state.

### 1c. Connect generously

A memory that is not linked to anything is half a memory. Every create/update:

- Add **outgoing wikilinks** to related memories: `[[Title]]`. Resolve by exact H1 title, case-insensitive.
- Run `mcp__bridgememory__suggest_connections` after creating and add strong matches as wikilinks under `## Related`.
- If a peer memory should link to the new one but doesn't, `mcp__bridgememory__append_to_memory` a back-reference under its `## Related`.

---

## 2. Create vs Append vs Update vs Delete

- **`create_memory`** — topic is new and self-contained. Search first to avoid duplicates. Stable declarative title (`"Russh pinning policy"`, not `"russh stuff"`). Lead with the claim; end with `## Related`.
- **`append_to_memory`** — topic exists; add a section/fact/link. Cheaper and safer than rewriting. Use a new `## Heading` so the addition has structure. **Do not parallelize on the same memory** — second call loses the first (read-modify-write race).
- **`update_memory`** — body is materially wrong, redundant, or restructured. Replaces the entire body. Read first, transform locally, write back.
- **Title rename** — only via the BridgeSpace UI. MCP tools do not rename; if you "update" the H1 in body, filename stays and `[[wikilinks]]` keep working by title.
- **`delete_memory`** — never on your own initiative. Only when the builder explicitly says delete.

---

## 3. Title and body conventions

- **Title** is the identity. Unique, declarative, stable. Reuse before coining — search first.
- **Filename** auto-slugified from title; never auto-renamed.
- **Body**:
  - First line: `# Title` (matches the title argument).
  - Lead paragraph: claim or conclusion. Builders skim.
  - Evidence: code blocks, `file:line` references, links to PRs/issues/commits.
  - `## Related` section at the bottom with wikilinks.
  - No YAML frontmatter, no inline tags. Plain prose.
- **Wikilinks**: `[[Title]]` or `[[Title|display text]]`.
- Keep memories **atomic** — one idea per file. > ~300 lines or multiple distinct ideas → split and link.

---

## 4. Fallback protocol — when MCP is unavailable

If `mcp__bridgememory__*` tools return errors (`token mismatch`, runtime unavailable, missing from tool list), **do not give up** — the hub is still on disk at `.bridgememory/`. See `references/fallback.md` for the full protocol.

Quick fallback:

```text
1. Confirm hub exists: ls C:\Users\nolan\CascadeProjects\fincept-terminal\.bridgememory\
2. List existing memories: ls .bridgememory/*.md
3. To read: Read tool on the .md file directly
4. To search: Grep across .bridgememory/*.md for distinctive tokens
5. To create: Write tool → path = .bridgememory/<Title>.md
6. To update/append: Edit tool on the existing .md
7. Never edit index.json or anything other than .md files
8. When MCP recovers, the .md files you wrote are already there — no migration needed
```

This is the documented behavior. BridgeMemory renders `.md` files in the folder; the MCP tools are the preferred interface but not the only one.

---

## 5. Hub-specific facts for this project

The Fincept Terminal hub was **initialized 2026-06-26** (see `.bridgememory/index.json`). It already contains ~30 interconnected memories organized around the `ml-dataset-evidence-spine` plan. Key entry points:

- `[[Fincept Evidence Hub — ml-dataset-evidence-spine]]` — directory of the 50 files in `.omo/evidence/`
- `[[Session Context — ml-dataset-evidence-spine]]` — architecture narrative
- `[[In-Depth Review — ml-dataset-evidence-spine]]` — verdict + audit rollup
- `[[Outstanding Findings Table]]` — 12 open items (1 HIGH, 4 MED, 7 LOW)
- `[[Update Protocol for Evidence Hub]]` — how to maintain the evidence hub specifically

For the broader project context, also recall:

- `[[value_increase.md canonical plan]]` — 60+ task IDs across 15 groups
- `[[Experimental fork of fincept-terminal]]` — sibling repo for parallel work
- `[[Experimental fork pre-work setup]]` — bootstrap protocol

See `references/hub-map.md` for the full memory inventory and `references/maintenance-checklist.md` for periodic hygiene.

---

## 6. Common mistakes to avoid

- **Asking permission to recall.** "Should I check BridgeMemory?" wastes a turn. Just check.
- **Creating a memory the builder will never see.** Bodies that read like internal monologue are noise.
- **Bypassing the hub for "context" the builder has clearly stored.** Search first.
- **Copy-pasting whole files into a memory.** Link to source path; summarize the *why*.
- **Inventing tags.** No tag system exists. `[[wikilinks]]` only.
- **Editing `index.json` or anything inside `.bridgememory/` other than `.md` files.** That's hub metadata.
- **Calling `delete_memory` to "clean up."** Orphans are intentional Zettelkasten state. Leave them.
- **Parallel `append_to_memory` on the same memory.** Read-modify-write race — sequence them.
- **Writing memories without outgoing wikilinks.** Half a memory. Connect generously.

---

## 7. Failure modes

- `hub_status` returns `null` → builder hasn't picked a hub. Operate without memory; don't pester.
- `mcp__bridgememory__*` missing from tool list → MCP server not installed. Use the fallback protocol; surface the gap once.
- `mcp__bridgememory__*` returns `token mismatch` → see §4 fallback.
- A search returns nothing on a question that should hit → trust the result; consider vocabulary mismatch; one synonym pass before giving up.

---

## 8. One-line distillation

> **Recall before you answer. Persist what's worth remembering. Connect generously. Do all three without being asked.**

If MCP is down, write `.md` files directly to `.bridgememory/`. The hub lives on disk, not in the MCP.