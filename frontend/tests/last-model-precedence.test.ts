import { describe, expect, it } from "vitest";
import { resolveRoleSelection } from "../src/components/NewSessionModal";
import { effortsForRuntime } from "../src/components/modelPicker";
import {
  makeProvider,
  makeRuntimeProfile,
  makeRuntimeProfilesSnapshot,
} from "./fixtures";

const profile = makeRuntimeProfile({ default_model: "default-model" });
const providers = [makeProvider()];

describe("resolveRoleSelection model precedence", () => {
  it("main role: backend last-used model outranks the locally-saved default", () => {
    const snapshot = makeRuntimeProfilesSnapshot({
      runtime_profiles: [profile],
      last_models: { "rp-1": "last-model" },
    });
    const r = resolveRoleSelection(
      { runtimeProfileId: "rp-1", model: "saved-model", reasoningEffort: "high", permission: {} },
      [profile],
      snapshot,
      providers,
      "main",
    );
    expect(r).toEqual({ runtimeProfileId: "rp-1", model: "last-model", reasoningEffort: "high", permission: {} });
  });

  it("worker role: saved default outranks backend last-used model (main usage must not override the worker pick)", () => {
    const snapshot = makeRuntimeProfilesSnapshot({
      runtime_profiles: [profile],
      last_models: { "rp-1": "last-model" },
    });
    const r = resolveRoleSelection(
      { runtimeProfileId: "rp-1", model: "saved-model", reasoningEffort: "high", permission: {} },
      [profile],
      snapshot,
      providers,
      "worker",
    );
    expect(r).toEqual({ runtimeProfileId: "rp-1", model: "saved-model", reasoningEffort: "high", permission: {} });
  });

  it("worker role falls back to last-used model when nothing is saved for the profile", () => {
    const snapshot = makeRuntimeProfilesSnapshot({
      runtime_profiles: [profile],
      last_models: { "rp-1": "last-model" },
    });
    const r = resolveRoleSelection(
      { runtimeProfileId: "rp-other", model: "irrelevant", reasoningEffort: "low", permission: {} },
      [profile],
      snapshot,
      providers,
      "worker",
    );
    expect(r).toEqual({ runtimeProfileId: "rp-1", model: "last-model", reasoningEffort: "medium", permission: {} });
  });

  it.each(["main", "worker"] as const)(
    "%s role falls back to the profile default model when no last-used and no saved",
    (role) => {
      const snapshot = makeRuntimeProfilesSnapshot({ runtime_profiles: [profile] });
      const r = resolveRoleSelection(undefined, [profile], snapshot, providers, role);
      expect(r).toEqual({ runtimeProfileId: "rp-1", model: "default-model", reasoningEffort: "medium", permission: {} });
    },
  );

  it("defers last-used model validation to the authoritative catalog hook", () => {
    const snapshot = makeRuntimeProfilesSnapshot({
      runtime_profiles: [profile],
      last_models: { "rp-1": "retired-model" },
    });
    const r = resolveRoleSelection(undefined, [profile], snapshot, providers, "main");
    expect(r).toEqual({ runtimeProfileId: "rp-1", model: "retired-model", reasoningEffort: "medium", permission: {} });
  });

  it("main role: backend last-used effort outranks the locally-saved effort", () => {
    const snapshot = makeRuntimeProfilesSnapshot({
      runtime_profiles: [profile],
      last_reasoning_efforts: { "rp-1": "high" },
    });
    const r = resolveRoleSelection(
      { runtimeProfileId: "rp-1", model: "saved-model", reasoningEffort: "low", permission: {} },
      [profile],
      snapshot,
      providers,
      "main",
    );
    expect(r.reasoningEffort).toBe("high");
  });

  it("worker role: saved effort outranks backend last-used effort", () => {
    const snapshot = makeRuntimeProfilesSnapshot({
      runtime_profiles: [profile],
      last_reasoning_efforts: { "rp-1": "high" },
    });
    const r = resolveRoleSelection(
      { runtimeProfileId: "rp-1", model: "saved-model", reasoningEffort: "low", permission: {} },
      [profile],
      snapshot,
      providers,
      "worker",
    );
    expect(r.reasoningEffort).toBe("low");
  });

  it("resolves effort from the profile's runner profile", () => {
    const baProfile = makeRuntimeProfile({
      id: "rp-ba",
      runner: "better_agent_runner",
      default_reasoning_effort: "minimal",
    });
    const provider = makeProvider({
      runner_options: ["native", "better_agent_runner"],
      runner_profiles: [
        { runner: "native", reasoning_efforts: ["medium", "high"] },
        { runner: "better_agent_runner", reasoning_efforts: ["minimal", "low"] },
      ],
    });
    const snapshot = makeRuntimeProfilesSnapshot({
      runtime_profiles: [baProfile],
      default_runtime_profile_id: "rp-ba",
    });
    const r = resolveRoleSelection(undefined, [baProfile], snapshot, [provider], "worker");
    expect(r.runtimeProfileId).toBe("rp-ba");
    expect(r.reasoningEffort).toBe("minimal");
  });

  it("falls back to the default profile when a suspended provider backs the saved profile", () => {
    const suspendedProfile = makeRuntimeProfile({ id: "rp-sus", provider_id: "sus" });
    const snapshot = makeRuntimeProfilesSnapshot({
      runtime_profiles: [suspendedProfile, profile],
      default_runtime_profile_id: "rp-1",
    });
    const r = resolveRoleSelection(
      { runtimeProfileId: "rp-sus", model: "x", reasoningEffort: "low", permission: {} },
      [suspendedProfile, profile],
      snapshot,
      [makeProvider(), makeProvider({ id: "sus", suspended: true })],
      "main",
    );
    expect(r.runtimeProfileId).toBe("rp-1");
  });

  it("uses model-specific effort combinations when the catalog provides them", () => {
    const agy = makeProvider({
      kind: "agy",
      runner_options: ["native", "better_agent_runner"],
    });
    const profiles = [
      { runner: "better_agent_runner" as const, model: "agy-2.5-flash", reasoning_efforts: ["none", "minimal"] as const },
      { runner: "better_agent_runner" as const, model: "agy-3.5-flash", reasoning_efforts: ["minimal"] as const },
    ].map((entry) => ({ ...entry, reasoning_efforts: [...entry.reasoning_efforts] }));
    expect(effortsForRuntime(agy, "better_agent_runner", "agy-2.5-flash", profiles)).toContain("none");
    expect(effortsForRuntime(agy, "better_agent_runner", "agy-3.5-flash", profiles)).not.toContain("none");
  });
});
