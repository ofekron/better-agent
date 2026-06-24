// @vitest-environment happy-dom

import { describe, expect, it } from "vitest";
import { buildMessageImageUrl } from "../src/utils/messageImages";

describe("buildMessageImageUrl", () => {
  it("encodes generated session image path segments", () => {
    expect(
      buildMessageImageUrl(
        "74188118-184f-4121-8e9b-46d410f377c4",
        "66f579a1-f414-41e0-8715-6c22c85a97bb_0.jpg",
      ),
    ).toBe(
      "/api/sessions/74188118-184f-4121-8e9b-46d410f377c4/images/66f579a1-f414-41e0-8715-6c22c85a97bb_0.jpg",
    );
  });

  it("returns an empty url without both required parts", () => {
    expect(buildMessageImageUrl(undefined, "image.png")).toBe("");
    expect(buildMessageImageUrl("session", undefined)).toBe("");
  });

  it("does not put bearer tokens in image URLs", () => {
    localStorage.setItem("better_agent_auth_token", "token/value");
    try {
      expect(buildMessageImageUrl("session", "image.png")).toBe(
        "/api/sessions/session/images/image.png",
      );
    } finally {
      localStorage.removeItem("better_agent_auth_token");
    }
  });
});
