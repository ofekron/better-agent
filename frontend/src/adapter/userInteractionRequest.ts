// Single canonical parser from a `user_interaction` node's raw
// `UserInteractionPayloadWire.request` bag to the plain fields the
// interactive card cores (components/UserInteractionCards.tsx) need.
// Two callers:
//   - mapToRenderModel.ts's `mapUserInteractionEventToRequest` wraps this
//     into the legacy `UserInteractionRequest` union for the ChatMessage-
//     shaped card wrappers in components/Chat.tsx.
//   - surface/nodes/UserInteractionAction.tsx feeds the plain fields
//     straight to the card cores — no legacy request-union type crosses
//     into surface/.
// Keeping the field-reading logic here once means both paths stay in
// sync by construction instead of by discipline.

import type { UserInputCardCoreQuestion, MemoryProposalCardCoreFields } from "../components/UserInteractionCards";

const RECOGNIZED_KINDS = new Set(["input", "approval", "memory"]);

export function isRecognizedUserInteractionKind(kind: unknown): kind is "input" | "approval" | "memory" {
  return typeof kind === "string" && RECOGNIZED_KINDS.has(kind);
}

interface FieldsBase {
  requestId: string;
  appSessionId: string;
}

export type UserInteractionActionFields =
  | ({ kind: "input" } & FieldsBase & { questions: UserInputCardCoreQuestion[] })
  | ({ kind: "approval" } & FieldsBase & { prompt: string })
  | ({ kind: "memory" } & FieldsBase & { memoryProposal: MemoryProposalCardCoreFields });

/** `fallbackRequestId` is used when `request.request_id` is absent (the
 * node's own id is the caller's best fallback identity). Returns `null`
 * for an unrecognized `kind` or a malformed `request` bag. */
export function parseUserInteractionActionFields(
  kind: unknown,
  request: Record<string, unknown>,
  fallbackRequestId: string,
): UserInteractionActionFields | null {
  if (!isRecognizedUserInteractionKind(kind)) return null;
  const requestId = typeof request.request_id === "string" ? request.request_id : fallbackRequestId;
  const appSessionId = typeof request.app_session_id === "string" ? request.app_session_id : "";

  if (kind === "approval") {
    return { kind, requestId, appSessionId, prompt: typeof request.prompt === "string" ? request.prompt : "" };
  }
  if (kind === "memory") {
    const memoryProposal = request.memory_proposal as MemoryProposalCardCoreFields | undefined;
    if (!memoryProposal || typeof memoryProposal !== "object") return null;
    return { kind, requestId, appSessionId, memoryProposal };
  }
  return {
    kind,
    requestId,
    appSessionId,
    questions: Array.isArray(request.questions) ? (request.questions as UserInputCardCoreQuestion[]) : [],
  };
}
