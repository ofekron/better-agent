import type { ChatMessage, RunInfo } from "../types";

/** A message group is "running" while it has active backend runs OR its
 *  assistant message is streaming (not stopped). Single source of truth for
 *  both auto-collapse gating (Chat.tsx defaultCollapsed) and the in-group
 *  "Running…" indicator (MessageGroup). */
export function isGroupRunning(
  assistantMessage: ChatMessage | undefined,
  runs: RunInfo[] | undefined,
): boolean {
  if ((runs ?? []).length > 0) return true;
  return !!assistantMessage?.isStreaming && !assistantMessage?.stopped_at;
}
