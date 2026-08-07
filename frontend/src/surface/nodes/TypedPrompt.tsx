// `typed_prompt` node — chat-panel.md's TypedPrompt: renders exactly what
// `origin` supplied, verbatim (ADR 0006 §0 "Prompt fidelity"). `sent_text`
// is on-demand only (full-prompt affordance), never inline by default.

import { useTranslation } from "react-i18next";
import type { NodeWire, TypedPromptPayloadWire } from "../../adapter/wire";
import { Markdown } from "../leaf/Markdown";

export function TypedPromptView({ node }: { node: NodeWire }) {
  const { t } = useTranslation();
  const payload = node.payload as TypedPromptPayloadWire | null;
  if (!payload) return null;
  return (
    <div className="surface-prompt" data-testid="surface-typed-prompt" data-origin={payload.origin}>
      {payload.origin === "supervisor" ? (
        <span className="surface-prompt-supervisor-chip">{payload.text}</span>
      ) : (
        <Markdown text={payload.text} />
      )}
      {payload.attachments.length > 0 && (
        <div className="surface-prompt-attachments">
          {payload.attachments.map((a) => (
            <span key={a.ref} className="surface-prompt-attachment" title={a.media_type}>
              {a.name}
            </span>
          ))}
        </div>
      )}
      {node.status === "queued" ? <span className="surface-prompt-queued">{t("input.queuedLabel")}</span> : null}
    </div>
  );
}
