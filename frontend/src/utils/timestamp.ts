// Single "HH:MM:SS today, MM/DD HH:MM:SS older" formatter, shared by
// legacy's `fmtTime` (MessageBubble.tsx call sites: message headers,
// collapsed-turn summaries, action-lead groups — the `.timeline-event-time`
// class this format is always paired with) and the native surface's
// `leaf/Timestamp.tsx` (A3 restoration).
function formatDate(d: Date): string {
  const now = new Date();
  const isToday =
    d.getFullYear() === now.getFullYear() &&
    d.getMonth() === now.getMonth() &&
    d.getDate() === now.getDate();
  const time = d.toLocaleTimeString(undefined, { hour12: false });
  if (isToday) return time;
  const date = `${String(d.getMonth() + 1).padStart(2, "0")}/${String(d.getDate()).padStart(2, "0")}`;
  return `${date} ${time}`;
}

/** Format an ISO-ish timestamp string (legacy `ChatMessage.timestamp`
 *  shape). Returns null on falsy/unparseable input. */
export function fmtTime(ts: string | undefined): string | null {
  if (!ts) return null;
  try {
    return formatDate(new Date(ts));
  } catch {
    return null;
  }
}

/** Format a native `NodeWire.ts` — UNIX epoch SECONDS (float), backend
 *  `surface_contract/nodes.py`'s `ts: float`, same convention `state.ts`'s
 *  own `createdAtSeconds = Date.now() / 1000` uses. Same format as
 *  `fmtTime` above, just a different input unit. */
export function fmtNodeTimestamp(ts: number): string | null {
  try {
    return formatDate(new Date(ts * 1000));
  } catch {
    return null;
  }
}
