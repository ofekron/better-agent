// Generic "artificial section" rendering — the single shared native
// counterpart of legacy MessageBubble.tsx's `UserContentSegments`/
// `ArtificialSectionChip`/`InlineTagsCards`. Any text field that can carry
// a backend/frontend-injected XML-ish wrapper (`<system-reminder>`,
// `<verdict-prompt>`, `<file-comment>`, `<inline-tags>`, …) routes through
// here — ONE parser (src/utils/artificialSections.ts's
// `parseArtificialSections`), ONE renderer, reused by every native leaf
// that shows such text (TypedPromptView, SteeringMessageView) instead of
// each one growing its own partial reimplementation.
//
// CSS is reused verbatim from legacy (`.artificial-section-chip`/
// `-header`/`-caret`/`-label`/`-hint`/`-preview`/`-body`,
// `.inline-tags-cards`/`.comment-card.inline-tags-card`/
// `-card-anchor`/`-card-selected` — all pre-existing, no new rules).

import { useState, type ReactNode } from "react";
import { Markdown } from "./Markdown";
import {
  hasArtificialSections,
  parseArtificialSections,
  prettyTagLabel,
  tagPreview,
  UNWRAP_TAG,
  type Segment,
} from "../../utils/artificialSections";
import { parseInlineTagsBody } from "../../utils/inlineTagsPrompt";

/** The contents of an `<inline-tags>` envelope, as comment cards — same
 * DOM shape/class names as legacy's `InlineTagsCards`. Falls back to
 * recursing as ordinary artificial-section segments when the body doesn't
 * actually parse as inline-tag comments (mirrors legacy's own fallback). */
function InlineTagsCards({ body }: { body: string }) {
  const comments = parseInlineTagsBody(body);
  if (comments.length === 0) return <ArtificialSectionSegments text={body} />;
  return (
    <div className="inline-tags-cards">
      {comments.map((c, i) => {
        const fileHeader = c.file && c.range ? `${c.file}:${c.range}` : c.file ?? null;
        return (
          <div key={i} className="comment-card inline-tags-card">
            {fileHeader && <div className="inline-tags-card-anchor">{fileHeader}</div>}
            {c.selected && <div className="inline-tags-card-selected">{c.selected}</div>}
            <div className="comment-card-comment">{c.comment}</div>
          </div>
        );
      })}
    </div>
  );
}

/** Collapsible chip for one artificial section — same header/body
 * chrome+behavior as legacy's `ArtificialSectionChip` (auto-expanded only
 * for `inline-tags`; attribute hints from `path`/`range`/`role`/`mode`/
 * `verb`; recursive body for nested tags). */
function ArtificialSectionChip({ tag, attrs, inner }: { tag: string; attrs: Record<string, string>; inner: string }) {
  const [expanded, setExpanded] = useState(tag === "inline-tags");
  const label = prettyTagLabel(tag);
  const hintParts: string[] = [];
  for (const k of ["path", "range", "role", "mode", "verb"]) {
    const v = attrs[k];
    if (v) hintParts.push(k === "path" ? v : `${k}=${v}`);
  }
  const hint = hintParts.join(" ");
  const preview = expanded ? "" : tagPreview(inner);
  return (
    <div className={`artificial-section-chip artificial-section-${tag}${expanded ? " expanded" : ""}`}>
      <button
        type="button"
        className="artificial-section-header"
        onClick={() => setExpanded((v) => !v)}
        aria-expanded={expanded}
      >
        <span className="artificial-section-caret" aria-hidden="true">{expanded ? "▾" : "▸"}</span>
        <span className="artificial-section-label">{label}</span>
        {hint && <span className="artificial-section-hint">{hint}</span>}
        {!expanded && preview && <span className="artificial-section-preview">{preview}</span>}
      </button>
      {expanded && (
        <div className="artificial-section-body">
          {tag === "inline-tags" ? <InlineTagsCards body={inner} /> : <ArtificialSectionSegments text={inner} />}
        </div>
      )}
    </div>
  );
}

function SegmentView({ seg }: { seg: Segment }) {
  if (seg.kind === "text") {
    if (!seg.text.trim()) return null;
    return <Markdown text={seg.text} />;
  }
  // The unwrap tag holds the user's actual text — render inline, no chip.
  if (seg.tag === UNWRAP_TAG) return <ArtificialSectionSegments text={seg.inner} />;
  return <ArtificialSectionChip tag={seg.tag} attrs={seg.attrs} inner={seg.inner} />;
}

/** Splits `text` into ordered artificial-section segments and renders
 * each in place — plain text through `Markdown` unchanged, every
 * recognized tag as a collapsible chip (`inline-tags` as comment cards).
 * A no-tags string is the overwhelmingly common case, short-circuited to
 * a single `Markdown` call with no parsing overhead. */
export function ArtificialSectionSegments({ text }: { text: string }): ReactNode {
  if (!hasArtificialSections(text)) return <Markdown text={text} />;
  const segments = parseArtificialSections(text);
  return (
    <>
      {segments.map((seg, i) => (
        <SegmentView key={i} seg={seg} />
      ))}
    </>
  );
}
