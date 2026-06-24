/** Resolve the prompt text for an Ask-flow picker action (Choose /
 * Create-new). The user message can be an orphan/cancelled stub whose
 * `content` is empty, while the real query text survives on the
 * assistant message's `ask_result.prompt_preview` (the full prompt,
 * not a truncated preview). Prefer the live user content; fall back to
 * the ask result's preview so the action never no-ops on empty text. */
export function resolveAskPrompt(
  userMessageContent: string | undefined | null,
  promptPreview: string | undefined | null,
): string {
  const content = (userMessageContent ?? "").trim();
  if (content) return userMessageContent as string;
  return promptPreview ?? "";
}
