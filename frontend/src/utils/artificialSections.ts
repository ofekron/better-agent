/** Allowlist + parser for "artificial sections" — XML-ish tags that the
 *  backend (or the frontend send-path) wraps around contextual content
 *  injected into a user message. The frontend collapses these so the
 *  bubble visualizes only the user's real text by default.
 *
 *  Adding a new wrapped section anywhere in the project? Register its
 *  tag name and a human-friendly label here.
 *
 *  Special case: `user_prompt` is in the allowlist but its content is
 *  the user's actual text — the renderer unwraps it instead of showing
 *  a chip. */

export const KNOWN_TAGS: Record<string, string> = {
  // User's real text — unwrapped, never collapsed.
  user_prompt: "User prompt",

  // Manager mode bootstrap.
  system_bootstrap: "System bootstrap",
  known_workers: "Known workers",

  // System reminder (frontend open-files preamble; matches Claude Code's own).
  "system-reminder": "System reminder",

  // Supervisor verdict + review.
  "verdict-prompt": "Supervisor verdict prompt",
  "review-prompt": "Adversarial review prompt",
  "original-request": "Original user request",
  "agent-last-output": "Agent's last output",
  "agent-jsonl": "Agent jsonl",

  // Manager: worker prep.
  "worker-prep": "Worker prep",

  // Browser test boot.
  "browser-test-boot": "Browser test boot",

  // Cross-session delegated worker/task prompt.
  "delegated-task": "Delegated task",

  // Adv-sync forks.
  "adv-sync-brief": "Adv-sync brief",
  "adv-sync-exchange": "Adv-sync exchange",
  "text-under-review": "Text under review",
  "original-text": "Original text",
  "other-fork-reply": "Other fork reply",

  // Working-mode file comment.
  "file-comment": "File comment",

  // File editor session bootstrap.
  "file-editor-bootstrap": "File-editor bootstrap",
  "file-editor-add-file": "File added to editor",

  // Prompt-engineer session bootstrap.
  "prompt-eng-bootstrap": "Prompt-engineer bootstrap",

  // Frontend inline-tags preamble.
  "inline-tags": "Inline tags",

  // User interrupt marker.
  "user-interrupt": "User interrupt",

  // Rearranger.
  source_path: "Source path",
  messages_delta: "Messages delta",
  trace_steps_delta: "Trace steps delta",
};

/** The unwrap tag: content inside is treated as the user's real text and
 *  rendered inline without a chip wrapper. */
export const UNWRAP_TAG = "user_prompt";

export type Segment =
  | { kind: "text"; text: string }
  | { kind: "tag"; tag: string; attrs: Record<string, string>; inner: string };

const TAG_ALTERNATION = Object.keys(KNOWN_TAGS)
  // Escape regex metacharacters (none in our names today, but defensive).
  .map((t) => t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"))
  .join("|");

// <tag attrs>inner</tag> — non-greedy inner so nested same-tag pairs
// (which shouldn't occur in this allowlist) wouldn't cross-match.
const TAG_RE = new RegExp(
  `<(${TAG_ALTERNATION})((?:\\s+[\\w-]+=("[^"]*"|'[^']*'|[^\\s>]*))*)\\s*>([\\s\\S]*?)<\\/\\1\\s*>`,
  "g",
);

const ATTR_RE = /([\w-]+)=(?:"([^"]*)"|'([^']*)'|([^\s>]+))/g;

function parseAttrs(s: string): Record<string, string> {
  const out: Record<string, string> = {};
  if (!s) return out;
  ATTR_RE.lastIndex = 0;
  let m: RegExpExecArray | null;
  while ((m = ATTR_RE.exec(s)) !== null) {
    out[m[1]] = m[2] ?? m[3] ?? m[4] ?? "";
  }
  return out;
}

/** Split content into ordered text/tag segments. Tag matching is flat
 *  (top-level only at this layer); nested allowed tags inside a tag's
 *  inner are surfaced by recursing on `inner` in the renderer. */
export function parseArtificialSections(content: string): Segment[] {
  if (!content) return [];
  const out: Segment[] = [];
  let lastIdx = 0;
  // Local copy so concurrent calls don't share lastIndex state.
  const re = new RegExp(TAG_RE.source, "g");
  let m: RegExpExecArray | null;
  while ((m = re.exec(content)) !== null) {
    if (m.index > lastIdx) {
      out.push({ kind: "text", text: content.slice(lastIdx, m.index) });
    }
    out.push({
      kind: "tag",
      tag: m[1],
      attrs: parseAttrs(m[2] || ""),
      inner: m[4] ?? "",
    });
    lastIdx = m.index + m[0].length;
  }
  if (lastIdx < content.length) {
    out.push({ kind: "text", text: content.slice(lastIdx) });
  }
  return out;
}

/** Returns true if the content contains at least one allowed tag. */
export function hasArtificialSections(content: string): boolean {
  if (!content) return false;
  const re = new RegExp(TAG_RE.source);
  return re.test(content);
}

/** Human-friendly label for a tag chip header. Falls back to the raw
 *  tag if it's not registered (should never happen for parsed segments
 *  since the parser only matches allowlisted tags). */
export function prettyTagLabel(tag: string): string {
  return KNOWN_TAGS[tag] ?? tag;
}

/** One-line preview for the collapsed chip header. */
export function tagPreview(inner: string, max = 80): string {
  const flat = inner.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim();
  if (!flat) return "";
  return flat.length > max ? flat.slice(0, max - 1) + "…" : flat;
}
