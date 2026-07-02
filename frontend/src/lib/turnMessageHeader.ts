/**
 * A turn-initiating message carries `source` only when it was injected programmatically
 * (supervisor verdict, worker delegation, mssg/ask team message, scheduler,
 * agent-board, another session, …) — a genuine user-typed turn has no
 * `source`. Map a known source to its display icon+label; for any unknown
 * injected source, humanize the raw string. A source-bearing turn initiator must
 * NEVER render as "User".
 */
const INJECTED_SOURCE_LABELS: Record<string, { icon: string; label: string }> = {
  supervisor: { icon: "🔍", label: "Supervisor" },
  worker: { icon: "⚙", label: "Worker" },
  mssg: { icon: "✉", label: "Message" },
  team_ask: { icon: "✉", label: "Ask" },
  schedule: { icon: "⏰", label: "Schedule" },
  "agent-board": { icon: "📋", label: "Agent Board" },
  adv_sync: { icon: "⚖", label: "Adversarial" },
  provisioning: { icon: "⚙", label: "Provisioning" },
  subprocess_agent: { icon: "🤖", label: "Agent" },
  assistant: { icon: "🤖", label: "Assistant" },
};

function humanizeSource(source: string): string {
  return source
    .split(/[._\-/]/)
    .filter(Boolean)
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(" ");
}

/** Header icon+label for a turn initiator, by its injection `source`. */
export function turnMessageHeader(source?: string): { icon: string; label: string } {
  if (!source) return { icon: "\u{1F464}", label: "User" };
  if (INJECTED_SOURCE_LABELS[source]) return INJECTED_SOURCE_LABELS[source];
  // Group source families like "supervisor.await_user" under the base label.
  const base = source.split(/[.]/)[0];
  if (INJECTED_SOURCE_LABELS[base]) return INJECTED_SOURCE_LABELS[base];
  // Never emit a blank label: a delimiter-only source humanizes to "".
  return { icon: "✉", label: humanizeSource(source) || source };
}
