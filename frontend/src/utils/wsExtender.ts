import type { WSEvent } from "../types";

// Predicate factory for inflight-op WS extenders: keeps an operation
// in-flight until a WS event whose `data.app_session_id` matches
// `targetSid` AND whose `type` is one of `types` arrives.
export function makeSessionExtender(
  targetSid: string,
  ...types: WSEvent["type"][]
): (ev: WSEvent) => boolean {
  return (ev) => {
    const sid = (ev.data as { app_session_id?: string } | undefined)
      ?.app_session_id;
    return sid === targetSid && types.includes(ev.type);
  };
}
