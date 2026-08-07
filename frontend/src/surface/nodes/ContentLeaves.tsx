// Plain-text/markdown leaf kinds: `assistant_text`, `thinking`,
// `steering_message`. No collapse state of their own (render invariant:
// "Prompt text, explanation text, and result content retain their
// canonical renderer and have independent folding controls" — these ARE
// that canonical renderer).

import { useTranslation } from "react-i18next";
import type {
  AssistantTextPayloadWire,
  NodeWire,
  SteeringMessagePayloadWire,
  ThinkingPayloadWire,
} from "../../adapter/wire";
import { Markdown } from "../leaf/Markdown";
import { CollapsibleBlock } from "../leaf/Collapsible";

export function AssistantTextView({ node }: { node: NodeWire }) {
  const payload = node.payload as AssistantTextPayloadWire | null;
  if (!payload || !payload.text) return null;
  return (
    <div className="surface-assistant-text" data-testid="surface-assistant-text" data-status={node.status ?? undefined}>
      <Markdown text={payload.text} />
    </div>
  );
}

export function ThinkingView({ node }: { node: NodeWire }) {
  const { t } = useTranslation();
  const payload = node.payload as ThinkingPayloadWire | null;
  if (!payload) return null;
  if (payload.redacted) {
    return <div className="surface-thinking-redacted">{t("thinking.thinking")}</div>;
  }
  return (
    <CollapsibleBlock header={<span>{t("thinking.thinking")}</span>} className="surface-thinking" testId="surface-thinking">
      <Markdown text={payload.text} />
    </CollapsibleBlock>
  );
}

export function SteeringMessageView({ node }: { node: NodeWire }) {
  const payload = node.payload as SteeringMessagePayloadWire | null;
  if (!payload) return null;
  return (
    <div className="event-steer-prompt" data-testid="surface-steering-message">
      <span className="event-steer-label">Steer</span>
      <span className="event-steer-text">
        <Markdown text={payload.text} />
      </span>
    </div>
  );
}
