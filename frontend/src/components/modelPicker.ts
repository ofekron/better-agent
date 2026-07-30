import type {
  Permission,
  Provider,
  ProviderRunner,
  ReasoningEffort,
  RuntimeProfile,
  RuntimeProfilesSnapshot,
  Session,
} from "../types";
import { wireHarnessProfileId } from "../lib/harnessProfile";

export type SelectorUpdates = Partial<
  Pick<Session, "runtime_profile_id" | "model" | "reasoning_effort" | "permission" | "harness_profile_id">
>;

export interface SelectorDraft {
  runtime_profile_id: string;
  model: string;
  reasoning_effort: ReasoningEffort | "";
  permission: Permission;
  harness_profile_id: string;
}

export interface ModelRuntimeProfile {
  runner: ProviderRunner;
  model: string;
  reasoning_efforts: ReasoningEffort[];
}

/** How a session resolves against the runtime-profile snapshot:
 * - `profile` set, `deleted` false — live profile (explicit id or legacy
 *   fallback match on the stamped (provider_id, runner)).
 * - `profile` set, `deleted` true — tombstoned profile; display its name
 *   with a deleted badge.
 * - `profile` null — legacy session with no matching profile; display the
 *   raw stamped provider/runner values. */
export interface SessionProfileView {
  profile: RuntimeProfile | null;
  deleted: boolean;
}

export function sessionProfileView(
  session: Pick<Session, "runtime_profile_id" | "provider_id" | "runner">,
  snapshot: RuntimeProfilesSnapshot | null,
): SessionProfileView {
  const all = snapshot?.runtime_profiles ?? [];
  if (session.runtime_profile_id) {
    const profile = all.find((p) => p.id === session.runtime_profile_id) ?? null;
    return { profile, deleted: !!profile?.deleted_at };
  }
  if (!session.provider_id) return { profile: null, deleted: false };
  const matched = all.find(
    (p) =>
      !p.deleted_at
      && p.provider_id === session.provider_id
      && (!session.runner || p.runner === session.runner),
  ) ?? null;
  return { profile: matched, deleted: false };
}

/** Prefill chain for a profile's model: last-used(profile) → profile default
 * → first catalog model. */
export function modelForProfile(
  profile: RuntimeProfile,
  snapshot: RuntimeProfilesSnapshot | null,
  models: string[],
): string {
  return (
    snapshot?.last_models?.[profile.id]
    || profile.default_model
    || models[0]
    || ""
  );
}

/** Prefill chain for a profile's reasoning effort, constrained to `options`:
 * last-used(profile) → profile default → first option. Empty when the
 * runtime has no effort axis. */
export function effortForProfile(
  profile: RuntimeProfile,
  snapshot: RuntimeProfilesSnapshot | null,
  options: ReasoningEffort[],
): ReasoningEffort | "" {
  if (!options.length) return "";
  const lastUsed = snapshot?.last_reasoning_efforts?.[profile.id];
  if (lastUsed && options.includes(lastUsed)) return lastUsed;
  if (options.includes(profile.default_reasoning_effort as ReasoningEffort)) {
    return profile.default_reasoning_effort;
  }
  return options[0];
}

/** Runtime kind a runner resolves to. `better_agent_runner` runs the
 *  BA-owned in-process loop for every kind EXCEPT claude: claude's
 *  better_agent_runner backend still speaks Claude's own wire format
 *  (Anthropic Messages API over the subscription OAuth token — see
 *  runner_better_agent_claude_subscription.py), so it resolves to the
 *  real `claude` runtime kind instead of collapsing into `openai` like
 *  every other kind's better_agent_runner choice does. The relative
 *  `native` runner always runs the provider's own engine, resolving to
 *  the provider's kind. Mirrors backend `runtime_profile.runtime_kind`. */
export function runtimeKindForRunner(
  providerKind: string,
  runner: ProviderRunner | string | null | undefined,
): string {
  if (runner === "better_agent_runner" && providerKind !== "claude") {
    return "openai";
  }
  return providerKind || "claude";
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
  runner: ProviderRunner | string | null | undefined,
): string {
  if (providerKind === "fugu" && runner !== "better_agent_runner") {
    return runtimeKindLabelKey("codex");
  }
  return runtimeKindLabelKey(runtimeKindForRunner(providerKind, runner));
}

export function effortsForRunner(
  provider: Provider,
  runner: ProviderRunner,
): ReasoningEffort[] {
  return provider.runner_profiles?.find((profile) => profile.runner === runner)?.reasoning_efforts
    ?? provider.reasoning_effort_options
    ?? [];
}

export function effortsForRuntime(
  provider: Provider,
  runner: ProviderRunner,
  model: string,
  profiles: ModelRuntimeProfile[],
): ReasoningEffort[] {
  return profiles.find((profile) => profile.runner === runner && profile.model === model)?.reasoning_efforts
    ?? effortsForRunner(provider, runner);
}

export function makeDraft(
  session: Session,
  snapshot: RuntimeProfilesSnapshot | null,
): SelectorDraft {
  const view = sessionProfileView(session, snapshot);
  return {
    runtime_profile_id: session.runtime_profile_id || view.profile?.id || "",
    model: session.model
      || (view.profile ? modelForProfile(view.profile, snapshot, []) : ""),
    reasoning_effort: session.reasoning_effort ?? "",
    permission: session.permission ?? {},
    harness_profile_id: session.harness_profile_id ?? "",
  };
}

export function changedUpdates(session: Session, draft: SelectorDraft): SelectorUpdates {
  const updates: SelectorUpdates = {};
  if (draft.runtime_profile_id && draft.runtime_profile_id !== (session.runtime_profile_id || "")) {
    updates.runtime_profile_id = draft.runtime_profile_id;
  }
  if (draft.model !== (session.model || "")) updates.model = draft.model;
  if (draft.reasoning_effort !== (session.reasoning_effort || "")) updates.reasoning_effort = draft.reasoning_effort;
  if (JSON.stringify(draft.permission) !== JSON.stringify(session.permission ?? {})) updates.permission = draft.permission;
  if (draft.harness_profile_id !== (session.harness_profile_id || "")) {
    updates.harness_profile_id = wireHarnessProfileId(draft.harness_profile_id) ?? "";
  }
  return updates;
}
