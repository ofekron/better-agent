import { describe, expect, it } from "vitest";

import {
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
  default_permission: {},
  suspended: false,
} as unknown as Provider);

describe("provider authority payloads", () => {
  it("binds record mutations to the rendered provider revision", () => {
    expect(providerAuthority(provider("target", "generation-a", 4))).toEqual({
      expected_generation: "generation-a",
      expected_revision: 4,
    });
  });

  it("refuses authority payloads for absent providers", () => {
    expect(() => requireProvider([], "missing")).toThrow("provider is unavailable");
  });
});
