import type { Permission, Provider, ReasoningEffort, Session } from "../types";

export type SelectorUpdates = Partial<
  Pick<Session, "provider_id" | "model" | "reasoning_effort" | "permission">
>;

export interface SelectorDraft {
  provider_id: string;
  model: string;
  reasoning_effort: ReasoningEffort | "";
  permission: Permission;
}

export function makeDraft(session: Session, providerId: string, providers: Provider[]): SelectorDraft {
  const provider = providers.find((p) => p.id === providerId);
  return {
    provider_id: providerId,
    model: session.model || provider?.last_model || provider?.default_model || "",
    reasoning_effort: session.reasoning_effort ?? "",
    permission: session.permission ?? {},
  };
}

export function modelForProvider(provider: Provider, models: string[]): string {
  return provider.last_model || provider.default_model || models[0] || "";
}

export function changedUpdates(session: Session, draft: SelectorDraft): SelectorUpdates {
  const updates: SelectorUpdates = {};
  if (draft.provider_id !== (session.provider_id || "")) updates.provider_id = draft.provider_id;
  if (draft.model !== (session.model || "")) updates.model = draft.model;
  if (draft.reasoning_effort !== (session.reasoning_effort || "")) updates.reasoning_effort = draft.reasoning_effort;
  if (JSON.stringify(draft.permission) !== JSON.stringify(session.permission ?? {})) updates.permission = draft.permission;
  return updates;
}
