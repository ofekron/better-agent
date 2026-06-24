// @vitest-environment happy-dom

import { describe, expect, it } from "vitest";
import { iframeModuleUrl } from "../src/components/ExtensionSlots";

describe("iframeModuleUrl", () => {
  it("serves only backend package assets and never appends bearer tokens", () => {
    localStorage.setItem("better_agent_auth_token", "token/value");
    try {
      expect(
        iframeModuleUrl("/api/extensions/ofek-dev.marketplace/frontend/ui/index.html"),
      ).toBe(
        "/api/extensions/ofek-dev.marketplace/frontend/ui/index.html",
      );
      expect(() =>
        iframeModuleUrl("http://backend.test/api/extensions/ofek-dev.marketplace/frontend/ui/index.html"),
      ).toThrow(/backend-served package asset/);
      expect(() => iframeModuleUrl("/api/extensions")).toThrow(/extension frontend asset/);
      expect(() => iframeModuleUrl("/api/sessions/session/images/image.png")).toThrow(
        /extension frontend asset/,
      );
    } finally {
      localStorage.removeItem("better_agent_auth_token");
    }
  });
});
