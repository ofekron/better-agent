import type { ChatMessage, EntityBlock, OrchestrationMode, WSEvent } from "../types";

export interface OrchestrationStrategy {
  readonly mode: OrchestrationMode;

  /** Read the event list for an assistant message. */
  getEvents(message: ChatMessage): WSEvent[];

  /** Build entity blocks for the timeline, or undefined for legacy path. */
  buildEntityBlocks(message: ChatMessage, workers: ChatMessage["workers"]): EntityBlock[] | undefined;

  /** Apply a live WS event to the message, returning a new message. */
  applyLiveEvent(message: ChatMessage, event: WSEvent): ChatMessage;

  /** Whether to render the Manager scope wrapper around the stream. */
  hasScopeWrapper(message: ChatMessage): boolean;
}
