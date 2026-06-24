import { parseInlineTagsBody } from "./inlineTagsPrompt";
import type { ParsedInlineComment } from "./inlineTagsPrompt";

const TAGS_RE = /<inline-tags>([\s\S]*?)<\/inline-tags>\n?/g;
const REMINDER_RE = /<system-reminder>([\s\S]*?)<\/system-reminder>\n?/g;

export interface SplitPreview {
  comments: ParsedInlineComment[];
  /** User-visible text with inline-tags + system-reminder envelopes stripped. */
  userText: string;
  /** Raw leading envelope (inline-tags + system-reminder blocks and the
   *  separators up to the user text) that precedes the user prompt in the
   *  original preview. Empty when there is no envelope. Reconstruct edited
   *  content with {@link applyQueuedEdit}. */
  prefix: string;
}

/** Split a queued-prompt preview into the inline-tags section(s), the
 *  remaining user-visible text, and the raw leading envelope. Handles
 *  multiple `<inline-tags>` blocks (from merged queued prompts) and strips
 *  the `<system-reminder>` open-files preamble the backend injects. */
export function splitPreview(preview: string): SplitPreview {
  const tagMatches = [...preview.matchAll(TAGS_RE)];
  const reminderMatches = [...preview.matchAll(REMINDER_RE)];
  const comments = tagMatches.flatMap((m) => parseInlineTagsBody(m[1]));
  const stripped = preview.replace(TAGS_RE, "").replace(REMINDER_RE, "");
  const userText = stripped.trim();

  // No envelope at all → the whole preview is user text; nothing to preserve.
  if (tagMatches.length === 0 && reminderMatches.length === 0) {
    return { comments, userText: preview, prefix: "" };
  }

  // The envelope is a leading contiguous block (system-reminder / inline-tags
  // in either order). The user prompt is everything after the last envelope
  // block ends. Capture the prefix by that offset — unambiguous, and immune to
  // substring collisions with comment bodies or whitespace trimming.
  const envelopeEnd = Math.max(
    0,
    ...tagMatches.map((m) => m.index! + m[0].length),
    ...reminderMatches.map((m) => m.index! + m[0].length),
  );
  // The block-strip regex consumes only one trailing newline; extend the
  // prefix across the remaining whitespace gap so the original separator
  // before the user text is preserved on reconstruction.
  let userStart = envelopeEnd;
  while (userStart < preview.length && preview[userStart] === "\n") userStart++;
  return { comments, userText, prefix: preview.slice(0, userStart) };
}

/** Reconstruct the full queued-prompt content after the user edits the
 *  user-visible text in the banner. Preserves the inline-tags + system-reminder
 *  envelope so editing a tagged prompt does not drop its structured context. */
export function applyQueuedEdit(originalPreview: string, editedUserText: string): string {
  const { prefix } = splitPreview(originalPreview);
  return prefix + editedUserText;
}
