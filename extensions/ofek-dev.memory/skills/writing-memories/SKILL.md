---
name: writing-memories
description: Use when deciding whether/how to save a durable fact about the user, their feedback, a project, or a reference via propose_memory_add/propose_memory_edit. Do not use for one-off task state, code patterns derivable from the repo, or anything already covered by requirement memory.
---

# Writing memories

This extension's MCP tools (`propose_memory_add`, `propose_memory_edit`,
`get_memories`) are the ONE memory system every provider (Claude, Codex,
Gemini) uses in this product. Never write memory files by hand or rely on a
provider's own built-in memory feature instead — always go through these
tools so every addition is user-reviewed and every provider sees the same
store.

## Before proposing anything

Ask: will this be true and useful next time, in a DIFFERENT conversation?
If the answer only holds for the rest of this turn, it is not a memory —
just keep working.

**Save it** when it is a durable fact that isn't derivable by re-reading the
code/git history next time:
- A user preference, role, or communication style they stated or you can
  infer with confidence ("terse responses", "senior Go engineer, new to
  React").
- Explicit feedback — a correction ("don't mock the DB in these tests") or a
  confirmed judgment call ("yes, one bundled PR was right here").
- A project fact not visible in the code: why a decision was made, a
  deadline, who owns what, an incident that shaped the current design.
- A pointer to an external system (a Linear project, a dashboard URL) with
  its purpose.

**Don't save:**
- Anything `git log`, `git blame`, or reading the current code already
  answers — architecture, file layout, conventions. Re-derive, don't cache.
- One-off task state, in-progress plans, or conversation-scoped context —
  that belongs in a plan/todo tool, not memory.
- A bug's fix recipe — the fix lives in the code and the commit message.
- Anything already covered by this product's requirement-memory system, if
  one is running in this project — don't duplicate it here.
- Sensitive personal data (health, financial account numbers, credentials)
  unless the user explicitly asks you to store it.
- Anything that will silently rot: don't phrase a memory in terms of "today"
  or "currently" if it'll be read weeks later — write the actual date, and
  write facts that stay true, not a snapshot of transient state.

## Picking a type

One of exactly four, matching how this store organizes and later retrieves
memories:

- `user` — who they are, their role, expertise, how they want to collaborate.
- `feedback` — a correction or confirmation about HOW to do the work. Always
  include *why* (the reasoning or incident that produced it) so it can be
  judged in edge cases, not blindly applied.
- `project` — durable facts about ongoing work: decisions, deadlines,
  incidents, ownership. Convert relative dates ("Thursday") to absolute ones
  before proposing.
- `reference` — a pointer to an external system and what it's for.

## Picking a scope

Every memory lives in exactly one scope, and you (the proposer) only choose
the SUGGESTED scope — the user can change it before approving:

- `global` — true regardless of which project/repo you're in (a user
  preference, a cross-cutting feedback rule).
- `project` — true for one repository but not others; pass the absolute
  repo root as `suggested_scope_path`.
- `folder` — true for one subtree of a repo (e.g. a package in a monorepo
  with its own conventions); pass that absolute path.

Default to the narrowest scope that's still correct. A memory scoped too
broadly leaks into unrelated work; scoped too narrowly, it won't be found
where it's needed. When genuinely unsure between `project` and `global`,
prefer `project` — the user sees and can widen it.

## Writing the content

- One memory, one fact (or one tightly related cluster). Don't bundle
  unrelated facts into a single entry — future retrieval and editing get
  harder.
- Lead with the fact/rule itself. For `feedback` and `project` types, follow
  with a **Why:** line (the reasoning or incident) and, for `feedback`, a
  **How to apply:** line (when this rule kicks in) — this is what lets a
  future read judge edge cases instead of applying the rule blindly.
- Keep it short. A memory that needs paragraphs to explain is usually two
  memories, or belongs in project documentation instead.
- Link related memories by name — `[[other-memory-name]]` — rather than
  repeating their content. Keep links one level deep: point directly at the
  file that has the detail, don't chain memory A → memory B → memory C.
- Use one consistent term for a concept across all your memories. Synonyms
  drift and make later search/dedup unreliable.
- `name` is a lowercase-kebab-case slug (e.g. `shared-git-index-races`) —
  it's also the filename, so make it specific enough to disambiguate from
  other memories in the same scope.
- `description` is the one-line index hook shown in `MEMORY.md` and in the
  approval card — write it so a future skim tells you whether to open the
  full file.

## The approval flow

`propose_memory_add`/`propose_memory_edit` block until the user approves,
edits, or rejects your proposal in the chat UI — nothing is written until
then. Treat the fields you pass as a first draft, not a final answer: the
user may change the scope, tighten the description, or edit the content
before approving. Don't re-propose a rejected memory without new information
that changes the case for it.

Before calling `propose_memory_add`, consider calling `get_memories` for the
current cwd first — if an existing memory already covers this fact, propose
an edit to it (`propose_memory_edit`) instead of creating a near-duplicate.

## Reading memories

Call `get_memories(cwd)` to pull everything visible from where you're
working — global memories plus every project/folder scope that's an
ancestor of `cwd`. Do this early in a session when durable context would
change how you approach the task, not on every single tool call.
