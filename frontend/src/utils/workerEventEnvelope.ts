import type { WSEvent } from "../types";

const WORKER_ENVELOPE_TYPES = new Set<WSEvent["type"]>([
  "worker_start",
  "worker_event",
  "worker_complete",
]);

export function unwrapTypedAgentMessageEnvelope(event: WSEvent): WSEvent | null {
  if (event.type !== "agent_message") return null;
  const data = event.data as { type?: unknown; data?: unknown };
  const wrappedType = data?.type;
  if (wrappedType === "event") {
    const message = (event.data as { message?: unknown }).message;
    if (!message || typeof message !== "object" || Array.isArray(message)) {
      return null;
    }
    const rawEvent = message as { type?: unknown; payload?: unknown; data?: unknown };
    const rawType = rawEvent.type;
    const rawData = rawEvent.payload ?? rawEvent.data;
    if (
      typeof rawType !== "string" ||
      !rawData ||
      typeof rawData !== "object" ||
      Array.isArray(rawData)
    ) {
      return null;
    }
    if (
      rawType !== "agent_message" &&
      !WORKER_ENVELOPE_TYPES.has(rawType as WSEvent["type"])
    ) {
      return null;
    }
    return {
      type: rawType as WSEvent["type"],
      data: rawData as WSEvent["data"],
    };
  }
  if (
    typeof wrappedType !== "string" ||
    !data.data ||
    typeof data.data !== "object" ||
    Array.isArray(data.data)
  ) {
    return null;
  }
  if (
    wrappedType !== "agent_message" &&
    !WORKER_ENVELOPE_TYPES.has(wrappedType as WSEvent["type"])
  ) {
    return null;
  }
  return {
    type: wrappedType as WSEvent["type"],
    data: data.data as WSEvent["data"],
  };
}

export function unwrapWorkerEventEnvelope(event: WSEvent): WSEvent | null {
  const unwrapped = unwrapTypedAgentMessageEnvelope(event);
  if (!unwrapped || !WORKER_ENVELOPE_TYPES.has(unwrapped.type)) return null;
  return unwrapped;
}
