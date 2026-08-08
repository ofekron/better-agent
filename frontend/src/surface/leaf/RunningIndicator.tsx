// Live "this turn's run is currently active" indicator. Reuses legacy
// MessageBubble.tsx's RunBadge visual language 1:1 (`.run-badge`/
// `.run-badge-pulse`/`.run-badge-label` CSS, `runBadge.running` i18n key
// across all locales) so the migration is visually consistent with what
// legacy showed, but is driven by the native wire's turn-level signal
// instead of legacy's backend `run_state.runs[]` list: `TurnEntry.phase`
// (state.ts), sourced from `turn_lifecycle` frames, is already the
// authoritative "is this turn's run active" fact (state.ts's
// `isLivePhase` — used today only to decide whether to fetch/show live
// body content, never rendered).
//
// `kind` (B1/B3 parity restoration) is an OPTIONAL, purely presentational
// prop — this component does no run-state lookup of its own. The one
// caller that needs the distinguishable manager/worker/native label
// (TurnView.tsx, for the session's own top-level turn) resolves it via
// `useRunSummaryByTurn` and passes it in; SubAgentTurnView's own nested
// live indicator (Container.tsx) already sits right next to that panel's
// OWN kind-labeled chip (`badgeText`/`SUBAGENT_KIND_LABEL_KEY`), so a
// second label there would be redundant — it omits `kind` and gets the
// same plain pulse as before. Legacy never i18n'd "manager"/"worker" (raw
// English words) — reproduced verbatim for parity, not translated here.

import { useTranslation } from "react-i18next";
import type { RunKindWire } from "../../adapter/wire";

const RUN_KIND_LABEL: Partial<Record<RunKindWire, string>> = {
  manager: "manager",
  worker: "worker",
  // "native" gets no label — legacy's own RunBadge.tsx computed "" for it.
};

export function RunningIndicator({ kind }: { kind?: RunKindWire | null }) {
  const { t } = useTranslation();
  const kindLabel = kind ? RUN_KIND_LABEL[kind] : undefined;
  return (
    <span className="run-badge" data-testid="surface-run-badge" data-kind={kind ?? undefined}>
      <span className="run-badge-pulse" aria-hidden="true" />
      <span className="run-badge-label">
        {kindLabel ? `${kindLabel} ${t("runBadge.running")}` : t("runBadge.running")}
      </span>
    </span>
  );
}
