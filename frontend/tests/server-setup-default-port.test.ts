import { describe, expect, it } from "vitest";
import { DEFAULT_BACKEND_PORT, normalizeServerUrl } from "../src/nativeServerConfig";

describe("ServerSetup", () => {
  it("uses the backend default port for bare HTTP hosts", () => {
    expect(normalizeServerUrl("192.168.1.20")).toBe(
      `http://192.168.1.20:${DEFAULT_BACKEND_PORT}`,
    );
  });

  it("preserves explicit ports", () => {
    expect(normalizeServerUrl("http://192.168.1.20:9000")).toBe(
      "http://192.168.1.20:9000",
    );
  });
});
