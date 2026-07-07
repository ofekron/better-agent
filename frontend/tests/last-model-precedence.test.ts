import { describe, expect, it } from "vitest";
import { resolveRuntimeProfile } from "../src/components/NewSessionModal";
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
      { providerId: "p1", model: "saved-model", reasoningEffort: "high" },
      [provider({ last_model: "last-model" })],
      "p1",
      MODELS,
      "main",
    );
    expect(r).toEqual({ providerId: "p1", model: "last-model", reasoningEffort: "high", permission: {} });
  });

  it("worker role: saved default outranks backend last_model (main usage must not override the worker pick)", () => {
    const r = resolveRuntimeProfile(
      { providerId: "p1", model: "saved-model", reasoningEffort: "high" },
      [provider({ last_model: "last-model" })],
      "p1",
      MODELS,
      "worker",
    );
    expect(r).toEqual({ providerId: "p1", model: "saved-model", reasoningEffort: "high", permission: {} });
  });

  it("worker role falls back to last_model when nothing is saved for the provider", () => {
    const r = resolveRuntimeProfile(
      { providerId: "other", model: "irrelevant", reasoningEffort: "low" },
      [provider({ id: "p1", last_model: "last-model" })],
      "p1",
      MODELS,
      "worker",
    );
    expect(r).toEqual({ providerId: "p1", model: "last-model", reasoningEffort: "medium", permission: {} });
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
      expect(r).toEqual({ providerId: "p1", model: "default-model", reasoningEffort: "medium", permission: {} });
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
    expect(r).toEqual({ providerId: "p1", model: "default-model", reasoningEffort: "medium", permission: {} });
  });

  it("accepts last_model unvalidated when the model list is empty (not yet fetched)", () => {
    const r = resolveRuntimeProfile(
      undefined,
      [provider({ last_model: "last-model" })],
      "p1",
      {},
      "main",
    );
    expect(r).toEqual({ providerId: "p1", model: "last-model", reasoningEffort: "medium", permission: {} });
  });

  it("main role: backend last_reasoning_effort outranks the locally-saved effort", () => {
    const r = resolveRuntimeProfile(
      { providerId: "p1", model: "saved-model", reasoningEffort: "low" },
      [provider({ last_reasoning_effort: "high" })],
      "p1",
      MODELS,
      "main",
    );
    expect(r.reasoningEffort).toBe("high");
  });

  it("worker role: saved effort outranks backend last_reasoning_effort", () => {
    const r = resolveRuntimeProfile(
      { providerId: "p1", model: "saved-model", reasoningEffort: "low" },
      [provider({ last_reasoning_effort: "high" })],
      "p1",
      MODELS,
      "worker",
    );
    expect(r.reasoningEffort).toBe("low");
  });
});
