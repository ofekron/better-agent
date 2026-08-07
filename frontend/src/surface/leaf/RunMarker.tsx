// Provider/model/effort marker chip attached to the last visible event of
// a contiguous run (markers.ts's computeRunMarkers) — same visual
// language/CSS classes/i18n keys as MessageBubble.tsx's RunMetaChips
// (`.run-meta-chips`/`.run-meta-chip`), taking a native RunWire directly
// instead of the legacy `ModelRunMeta` shape.

import { useTranslation } from "react-i18next";
import type { RunWire } from "../../adapter/wire";

export function RunMarker({ run }: { run: RunWire }) {
  const { t } = useTranslation();
  const parts: Array<{ key: string; label: string; value: string }> = [];
  if (run.provider_id) parts.push({ key: "provider", label: "message.provider", value: run.provider_id });
  if (run.model) parts.push({ key: "model", label: "message.model", value: run.model });
  if (run.reasoning_effort) parts.push({ key: "effort", label: "message.effort", value: run.reasoning_effort });
  if (run.runner) parts.push({ key: "runner", label: "message.runner", value: run.runner });
  if (parts.length === 0) return null;
  return (
    <span
      className="run-meta-chips"
      data-testid="surface-run-marker"
      title={parts.map((p) => `${t(p.label)}: ${p.value}`).join(" / ")}
    >
      {parts.map((p) => (
        <span className="run-meta-chip" key={p.key}>
          <span className="run-meta-chip-label">{t(p.label)}</span>
          <span className="run-meta-chip-value">{p.value}</span>
        </span>
      ))}
    </span>
  );
}
