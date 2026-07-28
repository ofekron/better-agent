import { describe, expect, it } from "vitest";

import {
  defaultProviderAuthority,
  providerAuthority,
  requireProvider,
} from "../src/providerAuthority";
import type { Provider } from "../src/types";

const provider = (id: string, generation: string, revision: number): Provider => ({
  id,
  generation,
  revision,
  name: id,
  kind: "claude",
  mode: "subscription",
  base_url: "",
  config_dir: "",
  custom_models: [],
  default_model: "",
  runner: "native",
  default_reasoning_effort: "",
  default_permission: {},
  suspended: false,
});

describe("provider authority payloads", () => {
  it("binds record mutations to the rendered provider revision", () => {
    expect(providerAuthority(provider("target", "generation-a", 4))).toEqual({
      expected_generation: "generation-a",
      expected_revision: 4,
    });
  });

  it("binds default changes to target and current default revisions", () => {
    const target = provider("target", "generation-target", 2);
    const current = provider("current", "generation-current", 7);
    expect(defaultProviderAuthority(target, [target, current], current.id)).toEqual({
      expected_generation: "generation-target",
      expected_revision: 2,
      expected_default_provider_id: "current",
      expected_default_generation: "generation-current",
      expected_default_revision: 7,
    });
  });

  it("refuses authority payloads for absent providers", () => {
    expect(() => requireProvider([], "missing")).toThrow("provider is unavailable");
    expect(() => defaultProviderAuthority(
      provider("target", "generation", 0),
      [],
      null,
    )).toThrow("current default provider is unavailable");
  });
});
