import { describe, expect, it } from "vitest";
import { resolveRuntimeProfile } from "../src/components/NewSessionModal";
import { effortsForRuntime } from "../src/components/modelPicker";
import type { Provider } from "../src/types";

function provider(overrides: Partial<Provider>): Provider {
  return {
    id: "p1",
    name: "Claude",
    kind: "claude",
    mode: "subscription",
    base_url: "",
    config_dir: "",
    custom_models: [],
    default_model: "default-model",
    runner: "native",
    runner_options: ["native"],
    runner_profiles: [{ runner: "native", reasoning_efforts: ["low", "medium", "high", "xhigh"] }],
    suspended: false,
    reasoning_effort_options: ["low", "medium", "high", "xhigh"],
    default_reasoning_effort: "medium",
    permission_options: {},
    default_permission: {},
    has_api_key: false,
    supports_fork: true,
    supports_manager_mode: true,
    supports_rewind: true,
    supports_steering: true,
    supports_native_subagents: false,
    supports_reasoning_effort: true,
    capability_overrides: {},
    ...overrides,
  };
}

const MODELS = {
  p1: ["default-model", "saved-model", "last-model"],
};

describe("resolveRuntimeProfile model precedence", () => {
  it("main role: backend last_model outranks the locally-saved default", () => {
    const r = resolveRuntimeProfile(
      { providerId: "p1", model: "saved-model", reasoningEffort: "high", runner: "native", permission: {} },
      [provider({ last_model: "last-model" })],
      "p1",
      MODELS,
      "main",
    );
    expect(r).toEqual({ providerId: "p1", model: "last-model", reasoningEffort: "high", runner: "native", permission: {} });
  });

  it("worker role: saved default outranks backend last_model (main usage must not override the worker pick)", () => {
    const r = resolveRuntimeProfile(
      { providerId: "p1", model: "saved-model", reasoningEffort: "high", runner: "native", permission: {} },
      [provider({ last_model: "last-model" })],
      "p1",
      MODELS,
      "worker",
    );
    expect(r).toEqual({ providerId: "p1", model: "saved-model", reasoningEffort: "high", runner: "native", permission: {} });
  });

  it("worker role falls back to last_model when nothing is saved for the provider", () => {
    const r = resolveRuntimeProfile(
      { providerId: "other", model: "irrelevant", reasoningEffort: "low", runner: "native", permission: {} },
      [provider({ id: "p1", last_model: "last-model" })],
      "p1",
      MODELS,
      "worker",
    );
    expect(r).toEqual({ providerId: "p1", model: "last-model", reasoningEffort: "medium", runner: "native", permission: {} });
  });

  it.each(["main", "worker"] as const)(
    "%s role falls back to default_model when no last_model and no saved",
    (role) => {
      const r = resolveRuntimeProfile(
        undefined,
        [provider({})],
        "p1",
        MODELS,
        role,
      );
      expect(r).toEqual({ providerId: "p1", model: "default-model", reasoningEffort: "medium", runner: "native", permission: {} });
    },
  );

  it("skips a stale last_model that is no longer in the provider's model list", () => {
    const r = resolveRuntimeProfile(
      undefined,
      [provider({ last_model: "retired-model" })],
      "p1",
      MODELS,
      "main",
    );
    expect(r).toEqual({ providerId: "p1", model: "default-model", reasoningEffort: "medium", runner: "native", permission: {} });
  });

  it("accepts last_model unvalidated when the model list is empty (not yet fetched)", () => {
    const r = resolveRuntimeProfile(
      undefined,
      [provider({ last_model: "last-model" })],
      "p1",
      {},
      "main",
    );
    expect(r).toEqual({ providerId: "p1", model: "last-model", reasoningEffort: "medium", runner: "native", permission: {} });
  });

  it("main role: backend last_reasoning_effort outranks the locally-saved effort", () => {
    const r = resolveRuntimeProfile(
      { providerId: "p1", model: "saved-model", reasoningEffort: "low", runner: "native", permission: {} },
      [provider({ last_reasoning_effort: "high" })],
      "p1",
      MODELS,
      "main",
    );
    expect(r.reasoningEffort).toBe("high");
  });

  it("worker role: saved effort outranks backend last_reasoning_effort", () => {
    const r = resolveRuntimeProfile(
      { providerId: "p1", model: "saved-model", reasoningEffort: "low", runner: "native", permission: {} },
      [provider({ last_reasoning_effort: "high" })],
      "p1",
      MODELS,
      "worker",
    );
    expect(r.reasoningEffort).toBe("low");
  });

  it("keeps a supported saved runner and resolves effort from that runner profile", () => {
    const r = resolveRuntimeProfile(
      {
        providerId: "p1",
        model: "saved-model",
        reasoningEffort: "minimal",
        runner: "better_agent_runner",
        permission: {},
      },
      [provider({
        runner_options: ["native", "better_agent_runner"],
        runner_profiles: [
          { runner: "native", reasoning_efforts: ["medium", "high"] },
          { runner: "better_agent_runner", reasoning_efforts: ["minimal", "low"] },
        ],
      })],
      "p1",
      MODELS,
      "worker",
    );
    expect(r.runner).toBe("better_agent_runner");
    expect(r.reasoningEffort).toBe("minimal");
  });

  it("uses model-specific effort combinations when the catalog provides them", () => {
    const gemini = provider({
      kind: "gemini",
      runner_options: ["native", "better_agent_runner"],
    });
    const profiles = [
      { runner: "better_agent_runner" as const, model: "gemini-2.5-flash", reasoning_efforts: ["none", "minimal"] as const },
      { runner: "better_agent_runner" as const, model: "gemini-3.5-flash", reasoning_efforts: ["minimal"] as const },
    ].map((profile) => ({ ...profile, reasoning_efforts: [...profile.reasoning_efforts] }));
    expect(effortsForRuntime(gemini, "better_agent_runner", "gemini-2.5-flash", profiles)).toContain("none");
    expect(effortsForRuntime(gemini, "better_agent_runner", "gemini-3.5-flash", profiles)).not.toContain("none");
  });
});
