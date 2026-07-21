import type { Permission, Provider, ReasoningEffort, Session } from "../types";

export type SelectorUpdates = Partial<
  Pick<Session, "provider_id" | "model" | "reasoning_effort" | "runner" | "permission">
>;

export interface SelectorDraft {
  provider_id: string;
  model: string;
  reasoning_effort: ReasoningEffort | "";
  runner: Provider["runner"];
  permission: Permission;
}

export interface ModelRuntimeProfile {
  runner: Provider["runner"];
  model: string;
  reasoning_efforts: ReasoningEffort[];
}

export function runnerForProvider(provider: Provider): Provider["runner"] {
  return provider.runner_options.includes(provider.runner)
    ? provider.runner
    : provider.runner_options[0] ?? provider.runner;
}

export function effortsForRunner(
  provider: Provider,
  runner: Provider["runner"],
): ReasoningEffort[] {
  return provider.runner_profiles?.find((profile) => profile.runner === runner)?.reasoning_efforts
    ?? provider.reasoning_effort_options
    ?? [];
}

export function effortsForRuntime(
  provider: Provider,
  runner: Provider["runner"],
  model: string,
  profiles: ModelRuntimeProfile[],
): ReasoningEffort[] {
  return profiles.find((profile) => profile.runner === runner && profile.model === model)?.reasoning_efforts
    ?? effortsForRunner(provider, runner);
}

export function makeDraft(session: Session, providerId: string, providers: Provider[]): SelectorDraft {
  const provider = providers.find((p) => p.id === providerId);
  return {
    provider_id: providerId,
    model: session.model || provider?.last_model || provider?.default_model || "",
    reasoning_effort: session.reasoning_effort ?? "",
    runner: session.runner || (provider ? runnerForProvider(provider) : "native"),
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
  if (draft.runner !== (session.runner || "")) updates.runner = draft.runner;
  if (JSON.stringify(draft.permission) !== JSON.stringify(session.permission ?? {})) updates.permission = draft.permission;
  return updates;
}
