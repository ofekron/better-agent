import { describe, expect, it } from "vitest";
import { isLeakedProviderMirror } from "../src/utils/modelDrift";
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
    reasoning_effort_options: ["low", "medium", "high", "xhigh"],
    default_reasoning_effort: "medium",
    has_api_key: false,
    supports_fork: true,
    supports_manager_mode: true,
    supports_rewind: true,
    supports_steering: true,
    supports_native_subagents: false,
    supports_reasoning_effort: true,
    ...overrides,
  };
}

const claude = provider({ id: "claude", name: "Claude", default_model: "opus" });
const zai = provider({
  id: "zai",
  name: "Z.AI",
  default_model: "glm-5.2",
  last_model: "glm-5.1",
});

describe("isLeakedProviderMirror", () => {
  it("suppresses the Z.AI default leaking onto a Claude session", () => {
    // The exact bug: default provider switched to Z.AI, its default_model
    // glm-5.2 sits in the global `model` mirror, session's provider is Claude.
    expect(isLeakedProviderMirror("glm-5.2", claude, zai)).toBe(true);
  });

  it("suppresses the default provider's last_model too", () => {
    expect(isLeakedProviderMirror("glm-5.1", claude, zai)).toBe(true);
  });

  it("does NOT suppress a legit model change within the same provider", () => {
    // currentProvider === defaultProvider → a real user selection, persist it.
    expect(isLeakedProviderMirror("glm-5.2", zai, zai)).toBe(false);
  });

  it("does NOT suppress a session model unrelated to the default mirror", () => {
    expect(isLeakedProviderMirror("opus", claude, zai)).toBe(false);
  });

  it("is inert when providers or model are missing", () => {
    expect(isLeakedProviderMirror("", claude, zai)).toBe(false);
    expect(isLeakedProviderMirror("glm-5.2", null, zai)).toBe(false);
    expect(isLeakedProviderMirror("glm-5.2", claude, null)).toBe(false);
  });
});
