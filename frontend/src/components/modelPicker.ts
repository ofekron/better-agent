import type { Permission, Provider, ReasoningEffort, Session } from "../types";
import { wireHarnessProfileId } from "../lib/harnessProfile";

export type SelectorUpdates = Partial<
  Pick<Session, "provider_id" | "model" | "reasoning_effort" | "runner" | "permission" | "harness_profile_id" | "harness_profile_revision">
>;

export interface SelectorDraft {
  provider_id: string;
  model: string;
  reasoning_effort: ReasoningEffort | "";
  runner: Provider["runner"];
  permission: Permission;
  harness_profile_id: string;
  harness_profile_revision: string;
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

/** Runtime kind a runner resolves to. `better_agent_runner` always runs the
 *  BA-owned OpenAI-compatible loop (runtime kind `openai`); the relative
 *  `native` runner runs the provider's own engine, so it resolves to the
 *  provider's kind. Mirrors backend `runtime_profile.runtime_kind`. */
export function runtimeKindForRunner(
  providerKind: string,
  runner: Provider["runner"] | string | null | undefined,
): string {
  return runner === "better_agent_runner" ? "openai" : (providerKind || "claude");
}

/** i18n key for a runtime kind's engine display name (e.g. "Claude",
 *  "Sakana Fugu", "Better Agent"). The single source for turning a
 *  runner into a real engine label instead of the relative "native". */
export function runtimeKindLabelKey(kind: string): string {
  return `runtimeKind.${kind || "claude"}`;
}

/** i18n key for the engine a (providerKind, runner) pair actually runs on.
 *  Display-only override: Fugu's `native` runner drives the plain `codex`
 *  binary (see `provider_manifest.py`'s `fugu.runner_module="runner_codex"`),
 *  so it's labeled "Codex" here even though `runtimeKindForRunner` keeps
 *  resolving to `fugu` for capability/permission/provider-class lookups,
 *  which are genuinely Fugu-specific (its own reasoning-effort options,
 *  `FuguProvider` class). */
export function runnerLabelKey(
  providerKind: string,
  runner: Provider["runner"] | string | null | undefined,
): string {
  if (providerKind === "fugu" && runner !== "better_agent_runner") {
    return runtimeKindLabelKey("codex");
  }
  return runtimeKindLabelKey(runtimeKindForRunner(providerKind, runner));
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
    harness_profile_id: session.harness_profile_id ?? "",
    harness_profile_revision: session.harness_profile_revision ?? "",
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
  if (
    draft.harness_profile_id !== (session.harness_profile_id || "")
    || draft.harness_profile_revision !== (session.harness_profile_revision || "")
  ) {
    const wireProfileId = wireHarnessProfileId(draft.harness_profile_id);
    updates.harness_profile_id = wireProfileId ?? "";
    updates.harness_profile_revision = wireProfileId ? draft.harness_profile_revision : "";
  }
  return updates;
}
