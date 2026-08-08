// Plain-text/markdown leaf kinds: `assistant_text`, `thinking`,
// `steering_message`. No collapse state of their own beyond what their
// reused legacy chrome already owns (render invariant: "Prompt text,
// explanation text, and result content retain their canonical renderer
// and have independent folding controls" — these ARE that canonical
// renderer).
//
// `assistant_text` reuses components/MessageBubble.tsx's `MessageBox`
// verbatim (already plain-props: text/defaultOpen/collapsible/onFileClick,
// no ChatMessage coupling) instead of a bare markdown div, so native
// assistant prose gets the SAME collapsible "💬 Message" box chrome
// (`.message-box`/`.message-box-toggle`/`.message-box-body`) legacy's
// `OutputEvent` renders it with — legacy's heavier `classifyOutput`
// dispatch (session/error/success/json/tool-result heuristics) doesn't
// apply here: the Contract-Node grammar already gives each of those
// semantic kinds its OWN node kind (result/failure/diagnostic/
// tool_interaction), so an `assistant_text` node is always genuine prose.
//
// `thinking` reuses components/ThinkingBlock.tsx verbatim (same
// `.thinking-block`/`.thinking-indicator`/`.thinking-content` CSS legacy
// uses) instead of the generic `.surface-collapsible` chrome, which has
// different glyphs/styling with no dedicated "thinking" theming.

import { lazy, Suspense } from "react";
import { useTranslation } from "react-i18next";
import type {
  AssistantTextPayloadWire,
  NodeWire,
  SteeringMessagePayloadWire,
  ThinkingPayloadWire,
} from "../../adapter/wire";
import { ArtificialSectionSegments } from "../leaf/ArtificialSections";
import { AttachmentChips } from "../leaf/AttachmentChips";

const MessageBox = lazy(() => import("../../components/MessageBubble").then((m) => ({ default: m.MessageBox })));
const ThinkingBlock = lazy(() => import("../../components/ThinkingBlock").then((m) => ({ default: m.ThinkingBlock })));

export function AssistantTextView({ node }: { node: NodeWire }) {
  const payload = node.payload as AssistantTextPayloadWire | null;
  if (!payload || !payload.text) return null;
  return (
    <div data-testid="surface-assistant-text" data-status={node.status ?? undefined}>
      <Suspense fallback={null}>
        <MessageBox text={payload.text} />
      </Suspense>
    </div>
  );
}

export function ThinkingView({ node }: { node: NodeWire }) {
  const { t } = useTranslation();
  const payload = node.payload as ThinkingPayloadWire | null;
  if (!payload || (!payload.text && !payload.redacted)) return null;
  if (payload.redacted) {
    return <div className="surface-thinking-redacted">{t("thinking.thinking")}</div>;
  }
  return (
    <Suspense fallback={null}>
      <ThinkingBlock thought={payload.text} />
    </Suspense>
  );
}

/** `steering_message` — legacy's `SteerPromptEvent`
 * (`.event-steer-prompt`/`-label`/`-text`, all pre-existing, no new CSS).
 * Text renders through the same shared artificial-section-chip renderer
 * as TypedPromptView (`leaf/ArtificialSections.tsx` — one parser, no
 * fork), matching legacy's `UserContentSegments` treatment.
 *
 * Attachments render through the same `leaf/AttachmentChips.tsx` renderer
 * TypedPromptView uses for `TypedPromptPayloadWire.attachments` — one
 * component, no fork. `SteeringMessagePayloadWire.attachments`
 * (backend/surface_contract/nodes.py's `SteeringMessagePayload`,
 * populated by `normalize._handle_steer_prompt` from the steer's saved
 * images) carries the exact same `Attachment` shape. */
export function SteeringMessageView({ node }: { node: NodeWire }) {
  const payload = node.payload as SteeringMessagePayloadWire | null;
  if (!payload) return null;
  return (
    <div className="event-steer-prompt" data-testid="surface-steering-message">
      <span className="event-steer-label">Steer</span>
      <span className="event-steer-text">
        <ArtificialSectionSegments text={payload.text} />
      </span>
      <AttachmentChips attachments={payload.attachments} sessionId={node.surface_id} />
    </div>
  );
}
