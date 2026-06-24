import type {
  ChatMessage,
  OrchestrationMode,
  WSEvent,
} from "../types";
import { getStrategy } from "../strategies";

function isSyntheticAssistantEvent(event: WSEvent): boolean {
  if (event.type === "manager_event") {
    const inner = (event.data as { event?: WSEvent } | undefined)?.event;
    return inner ? isSyntheticAssistantEvent(inner) : false;
  }
  if (event.type !== "agent_message") return false;
  const data = event.data as {
    type?: string;
    isApiErrorMessage?: boolean;
    message?: { model?: string };
  } | undefined;
  return (
    data?.type === "assistant" &&
    data.message?.model === "<synthetic>" &&
    !data.isApiErrorMessage
  );
}

/**
 * Apply one live WS turn-event onto the canonical assistant message.
 *
 * The mode comes from the owning session's `orchestration_mode`; the
 * single strategy is parameterized by it to route event handling and
 * entity rendering.
 */
export function applyLiveTurnEvent(
  msg: ChatMessage,
  event: WSEvent,
  mode: OrchestrationMode | undefined
): ChatMessage {
  if (isSyntheticAssistantEvent(event)) return msg;
  const strategy = getStrategy(mode);
  return strategy.applyLiveEvent(msg, event);
}
