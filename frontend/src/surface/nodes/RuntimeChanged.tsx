// `model_change` / `harness_change` — the RuntimeChanged family
// (chat-panel.md), rendered as a boundary before the turn they affect.
//
// `ModelChangePayloadWire.from_run_ref`/`to_run_ref` are display MODEL
// NAME STRINGS despite the `RunRef` field typing (see wire.ts's comment
// on the type) — no `runs[]` lookup needed here.

import { useTranslation } from "react-i18next";
import type { HarnessChangePayloadWire, ModelChangePayloadWire, NodeWire } from "../../adapter/wire";

export function ModelChangeView({ node }: { node: NodeWire }) {
  const { t } = useTranslation();
  const payload = node.payload as ModelChangePayloadWire | null;
  if (!payload) return null;
  return (
    <div className="event-model-switched" data-testid="surface-model-change" data-source={payload.source}>
      <span>{t("message.modelSwitched")}</span>
      <span>
        {payload.from_run_ref ? `${payload.from_run_ref} → ${payload.to_run_ref}` : payload.to_run_ref}
      </span>
    </div>
  );
}

export function HarnessChangeView({ node }: { node: NodeWire }) {
  const { t } = useTranslation();
  const payload = node.payload as HarnessChangePayloadWire | null;
  if (!payload) return null;
  return (
    <div className="event-model-switched" data-testid="surface-harness-change">
      <span>{t("message.harnessChanged")}</span>
      <span>
        {payload.from_harness_profile_id
          ? `${payload.from_harness_profile_id} → ${payload.to_harness_profile_id}`
          : payload.to_harness_profile_id}
      </span>
    </div>
  );
}
