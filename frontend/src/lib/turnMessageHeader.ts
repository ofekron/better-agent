/**
 * A turn-initiating message carries `source` only when it was injected programmatically
 * (supervisor verdict, worker delegation, mssg/ask team message, scheduler,
 * agent-board, another session, …) — a genuine user-typed turn has no
 * `source`. Map a known source to its display icon+label; for any unknown
 * injected source, humanize the raw string. A source-bearing turn initiator must
 * NEVER render as "User".
 */
// Turn-type accent colors, grouped by what the source represents rather
// than one hue per source — keeps the palette "slightly different", not a
// rainbow. Reuses the app's existing semantic hues (--agent for
// worker/agent turns, --warning for direct file edits) and adds two new
// ones (--turn-review, --turn-external) for the remaining groups; see
// :root in globals.css. A plain user turn (no source) keeps the default
// --accent, so it's the odd one out here.
const COLOR_WORKER = "var(--agent)";
const COLOR_OPERATOR = "var(--warning)";
const COLOR_REVIEW = "var(--turn-review)";
const COLOR_EXTERNAL = "var(--turn-external)";

const INJECTED_SOURCE_LABELS: Record<string, { icon: string; label: string; color: string }> = {
  supervisor: { icon: "🔍", label: "Supervisor", color: COLOR_REVIEW },
  worker: { icon: "⚙", label: "Worker", color: COLOR_WORKER },
  mssg: { icon: "✉", label: "Message", color: COLOR_EXTERNAL },
  team_ask: { icon: "✉", label: "Ask", color: COLOR_EXTERNAL },
  schedule: { icon: "⏰", label: "Schedule", color: COLOR_EXTERNAL },
  "agent-board": { icon: "📋", label: "Agent Board", color: COLOR_EXTERNAL },
  adv_sync: { icon: "⚖", label: "Adversarial", color: COLOR_REVIEW },
  provisioning: { icon: "⚙", label: "Provisioning", color: COLOR_EXTERNAL },
  subprocess_agent: { icon: "🤖", label: "Agent", color: COLOR_WORKER },
  assistant: { icon: "🤖", label: "Assistant", color: COLOR_WORKER },
  file_editor: { icon: "✎", label: "Operator", color: COLOR_OPERATOR },
  operator: { icon: "✎", label: "Operator", color: COLOR_OPERATOR },
};

function humanizeSource(source: string): string {
  return source
    .split(/[._\-/]/)
    .filter(Boolean)
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(" ");
}

const DEFAULT_USER_LABEL = "User";

function cleanUserLabel(label?: string | null): string {
  const cleaned = label?.trim().replace(/\s+/g, " ");
  return cleaned || DEFAULT_USER_LABEL;
}

/** Header icon+label+accent color for a turn initiator, by its injection `source`. */
export function turnMessageHeader(source?: string, userLabel?: string | null): { icon: string; label: string; color?: string } {
  if (!source) return { icon: "\u{1F464}", label: cleanUserLabel(userLabel) };
  if (INJECTED_SOURCE_LABELS[source]) return INJECTED_SOURCE_LABELS[source];
  // Group source families like "supervisor.await_user" under the base label.
  const base = source.split(/[.]/)[0];
  if (INJECTED_SOURCE_LABELS[base]) return INJECTED_SOURCE_LABELS[base];
  // Never emit a blank label: a delimiter-only source humanizes to "".
  return { icon: "✉", label: humanizeSource(source) || source, color: COLOR_EXTERNAL };
}
