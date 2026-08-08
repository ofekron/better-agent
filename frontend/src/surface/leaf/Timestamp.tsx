// `.message-box-footer > .user-message-time` — legacy's per-message
// timestamp footer (MessageBubble.tsx's `fmtTime` call sites: the prompt's
// own footer, an error/collapsed-response footer, and the final assistant
// response's footer — all the same idiom, right after that message's own
// content/status). Reused at parity for any native node carrying a wire
// `ts` (UNIX epoch seconds).

import { fmtNodeTimestamp } from "../../utils/timestamp";

export function Timestamp({ ts }: { ts: number }) {
  const formatted = fmtNodeTimestamp(ts);
  // Never shown as a placeholder for an unparseable timestamp — same as
  // legacy's own `if (!ts) return null`.
  if (!formatted) return null;
  return (
    <div className="message-box-footer">
      <span className="user-message-time">{formatted}</span>
    </div>
  );
}
