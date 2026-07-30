import { describe, expect, it } from "vitest";
import { isLeakedProfileMirror } from "../src/utils/modelDrift";
import { makeRuntimeProfile } from "./fixtures";

const zaiProfile = makeRuntimeProfile({
  id: "rp-zai",
  provider_id: "zai",
  name: "Z.AI",
  default_model: "glm-5.2",
});
const lastModels = { "rp-zai": "glm-5.1" };

describe("isLeakedProfileMirror", () => {
  it("suppresses the Z.AI default leaking onto a Claude session", () => {
    // The exact bug: default profile switched to Z.AI, its default_model
    // glm-5.2 sits in the global `model` mirror, session's provider is Claude.
    expect(isLeakedProfileMirror("glm-5.2", "claude", zaiProfile, lastModels)).toBe(true);
  });

  it("suppresses the default profile's last-used model too", () => {
    expect(isLeakedProfileMirror("glm-5.1", "claude", zaiProfile, lastModels)).toBe(true);
  });

  it("does NOT suppress a legit model change within the same provider", () => {
    // Session provider === default profile's provider → a real user
    // selection, persist it.
    expect(isLeakedProfileMirror("glm-5.2", "zai", zaiProfile, lastModels)).toBe(false);
  });

  it("does NOT suppress a session model unrelated to the default mirror", () => {
    expect(isLeakedProfileMirror("opus", "claude", zaiProfile, lastModels)).toBe(false);
  });

  it("is inert when profile, provider, or model are missing", () => {
    expect(isLeakedProfileMirror("", "claude", zaiProfile, lastModels)).toBe(false);
    expect(isLeakedProfileMirror("glm-5.2", undefined, zaiProfile, lastModels)).toBe(false);
    expect(isLeakedProfileMirror("glm-5.2", "claude", null, lastModels)).toBe(false);
  });
});
