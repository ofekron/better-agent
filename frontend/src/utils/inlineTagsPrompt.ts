import type { InlineTag } from "../types/inlineTag";

/** Encode a single inline comment as a compact XML element inside the
 * <inline-tags> envelope. Attributes carry the structured anchor (file
 * path + optional line/col range); free-form selected text and the
 * comment body live in child elements so they can hold multi-line
 * payloads safely (no quote escaping, no fences). The token budget per
 * comment is one open tag + attrs + one close tag + the actual
 * content — markedly cheaper than the prior "Selected text: ... User
 * comment: ..." prose-with-fences format. */
function renderTag(t: InlineTag): string {
  const attrs: string[] = [];
  if (t.fileAnchor) {
    attrs.push(`file="${t.fileAnchor.filePath}"`);
    const { startLine, endLine, startCol, endCol } = t.fileAnchor;
    if (
      startLine !== undefined &&
      endLine !== undefined &&
      startCol !== undefined &&
      endCol !== undefined
    ) {
      const range =
        startLine === endLine
          ? `${startLine}:${startCol}-${endCol}`
          : `${startLine}:${startCol}-${endLine}:${endCol}`;
      attrs.push(`range="${range}"`);
    }
  }
  const attrStr = attrs.length ? " " + attrs.join(" ") : "";
  const sel = t.selectedText
    ? `<sel>${t.selectedText}</sel>`
    : "";
  return `<c${attrStr}>${sel}${t.comment}</c>`;
}

export function buildInlineTagsPreamble(tags: InlineTag[]): string {
  if (tags.length === 0) return "";
  const body = tags.map(renderTag).join("\n");
  return `<inline-tags>\n${body}\n</inline-tags>\n`;
}

export function mergeTagsIntoPrompt(prompt: string, tags: InlineTag[]): string {
  const preamble = buildInlineTagsPreamble(tags);
  if (!preamble) return prompt;
  if (!prompt.trim()) return preamble;
  return preamble + "\n" + prompt;
}

/** Inverse of `renderTag` — parse the inside of an `<inline-tags>`
 *  envelope back into structured comment records so the message-bubble
 *  chip can render them as comment cards instead of raw XML. Returns
 *  the records in document order. */
export interface ParsedInlineComment {
  file?: string;
  range?: string;
  selected?: string;
  comment: string;
}

const C_RE = /<c\b([^>]*)>([\s\S]*?)<\/c\s*>/g;
const COMMENT_RE = /<comment\b([^>]*)>([\s\S]*?)<\/comment\s*>/g;
const SEL_RE = /<sel>([\s\S]*?)<\/sel\s*>/;
const ATTR_RE_LOCAL = /([\w-]+)=(?:"([^"]*)"|'([^']*)'|([^\s>]+))/g;

function parseAttrs(s: string): Record<string, string> {
  const out: Record<string, string> = {};
  ATTR_RE_LOCAL.lastIndex = 0;
  let m: RegExpExecArray | null;
  while ((m = ATTR_RE_LOCAL.exec(s)) !== null) {
    out[m[1]] = m[2] ?? m[3] ?? m[4] ?? "";
  }
  return out;
}

export function parseInlineTagsBody(body: string): ParsedInlineComment[] {
  const out: ParsedInlineComment[] = [];
  const re = new RegExp(`${C_RE.source}|${COMMENT_RE.source}`, "g");
  let m: RegExpExecArray | null;
  while ((m = re.exec(body)) !== null) {
    const attrs = parseAttrs(m[1] || m[3] || "");
    const inner = m[2] ?? m[4] ?? "";
    const selM = inner.match(SEL_RE);
    const selected = selM ? selM[1] : undefined;
    const comment = inner.replace(SEL_RE, "").trim();
    out.push({
      file: attrs.file || undefined,
      range: attrs.range || undefined,
      selected,
      comment,
    });
  }
  return out;
}
