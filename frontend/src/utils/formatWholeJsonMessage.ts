/**
 * Detect chat messages whose ENTIRE content is JSON or JSONL and rewrite them
 * as a pretty-printed ```json fenced block so the markdown renderer shows them
 * with syntax highlighting + monospace instead of a raw one-line blob. Used
 * for machine-generated messages (e.g. reviewer verdict payloads). Returns the
 * original text unchanged when the content isn't wholly JSON.
 */
const MAX_CHARS = 64 * 1024;

/** Parse only when the result is a JSON object or array (not null/scalars). */
function parseJsonObject(text: string): unknown | undefined {
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    return undefined;
  }
  return parsed !== null && typeof parsed === "object" ? parsed : undefined;
}

export function formatWholeJsonMessage(text: string): string {
  const trimmed = (text ?? "").trim();
  if (!trimmed || trimmed.length > MAX_CHARS) return text;

  const whole = parseJsonObject(trimmed);
  if (whole !== undefined) {
    return "```json\n" + JSON.stringify(whole, null, 2) + "\n```";
  }

  // JSONL: every non-empty line is itself a JSON object/array.
  const lines = trimmed.split("\n").filter((line) => line.trim().length > 0);
  if (lines.length >= 2) {
    const parsed = lines.map(parseJsonObject);
    if (parsed.every((value) => value !== undefined)) {
      const body = parsed
        .map((value) => JSON.stringify(value, null, 2))
        .join("\n\n");
      return "```json\n" + body + "\n```";
    }
  }

  return text;
}
