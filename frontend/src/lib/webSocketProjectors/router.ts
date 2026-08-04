import type { WSEvent } from "../../types";
import { projectPromptEvent } from "./promptProjector";
import { projectSessionEvent } from "./sessionProjector";
import type { WebSocketProjectorContext } from "./types";

export function routeAppWebSocketEvent(
  event: WSEvent,
  context: WebSocketProjectorContext,
): void | Promise<void> {
  const promptResult = projectPromptEvent(event, context);
  if (promptResult.stop) return promptResult.completion;
  return projectSessionEvent(event, context).completion;
}
