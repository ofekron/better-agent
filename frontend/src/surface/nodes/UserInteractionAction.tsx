// `user_interaction` node — a PENDING node whose `kind` is one of
// input/approval/memory renders the real interactive form (the same
// components/UserInteractionCards.tsx cores the legacy path uses,
// resolving through the SAME `/api/user-input/{request_id}/resolve|cancel`
// endpoints), instead of a passive state chip. Resolved/cancelled nodes,
// and any unrecognized `kind`, keep the read-only state chip
// (leaf/Chips.tsx's UserInteractionChip).
//
// `onDone` is intentionally a no-op here: resolving POSTs directly (no
// legacy-side bookkeeping to clear), and the backend's own follow-up
// `node_upsert` (state: resolved/cancelled) is what the store applies —
// this view then naturally re-renders through the `state !== "pending"`
// branch below with no local state to reconcile.
//
// As of Phase I stage 2a, no backend adapter (normalize.py/derive.py/
// chat_adapter.py) constructs a `user_interaction` NodeWire node yet
// (grep-confirmed, same gap mapToRenderModel.ts's legacy-path mapper
// already documents) — this renderer is forward-built against the
// contract so it's correct the moment the backend starts emitting these
// nodes, exactly like NodeView's pre-existing dispatch for the still-
// unproduced WORKER_TURN/SUB_SESSION_TURN/SESSION_TURN kinds.

import { lazy, Suspense } from "react";
import type { NodeWire, UserInteractionPayloadWire } from "../../adapter/wire";
import { parseUserInteractionActionFields } from "../../adapter/userInteractionRequest";
import { UserInteractionChip } from "../leaf/Chips";

const UserInputCardCore = lazy(() =>
  import("../../components/UserInteractionCards").then((m) => ({ default: m.UserInputCardCore })),
);
const UserApprovalCardCore = lazy(() =>
  import("../../components/UserInteractionCards").then((m) => ({ default: m.UserApprovalCardCore })),
);
const MemoryProposalCardCore = lazy(() =>
  import("../../components/UserInteractionCards").then((m) => ({ default: m.MemoryProposalCardCore })),
);

const NOOP_ON_DONE = () => {};

export function UserInteractionActionView({ node }: { node: NodeWire }) {
  const payload = node.payload as UserInteractionPayloadWire | null;
  if (!payload) return null;
  if (payload.state !== "pending") return <UserInteractionChip payload={payload} />;

  const fields = parseUserInteractionActionFields(payload.kind, payload.request, node.node_id);
  if (!fields) return <UserInteractionChip payload={payload} />;

  return (
    <Suspense fallback={null}>
      <div data-testid="surface-user-interaction-action" data-kind={fields.kind}>
        {fields.kind === "approval" ? (
          <UserApprovalCardCore
            requestId={fields.requestId}
            appSessionId={fields.appSessionId}
            prompt={fields.prompt}
            onDone={NOOP_ON_DONE}
          />
        ) : fields.kind === "memory" ? (
          <MemoryProposalCardCore
            requestId={fields.requestId}
            appSessionId={fields.appSessionId}
            memoryProposal={fields.memoryProposal}
            onDone={NOOP_ON_DONE}
          />
        ) : (
          <UserInputCardCore
            requestId={fields.requestId}
            appSessionId={fields.appSessionId}
            questions={fields.questions}
            onDone={NOOP_ON_DONE}
          />
        )}
      </div>
    </Suspense>
  );
}
