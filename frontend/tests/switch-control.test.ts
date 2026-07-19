import { describe, expect, it } from "vitest";

import {
  activeSwitchRequest,
  redirectUrlForLine,
  restartStatusForRequest,
  switchTargetUrl,
} from "../../extensions/switch-control/ui/switch.entry.js";

describe("Line Switch restart status", () => {
  it("reads the canonical top-level switch projection", () => {
    expect(restartStatusForRequest({ request_id: "r1", status: "succeeded" }, "r1")).toEqual({
      status: "succeeded",
      error: "",
    });
    expect(
      restartStatusForRequest({ request_id: "r1", status: "failed", error: "build failed" }, "r1"),
    ).toEqual({ status: "failed", error: "build failed" });
  });

  it("ignores a stale request and keeps pending requests pending", () => {
    expect(restartStatusForRequest({ request_id: "old", status: "succeeded" }, "new")).toEqual({
      status: "pending",
      error: "",
    });
    expect(restartStatusForRequest({ request_id: "r1", status: "accepted" }, "r1")).toEqual({
      status: "pending",
      error: "",
    });
  });
});

describe("Line Switch durable progress", () => {
  it("restores switching state from the backend request projection", () => {
    const request = { request_id: "r1", target: "dev", status: "accepted", error: "" };
    expect(activeSwitchRequest({ request })).toEqual(request);
    expect(activeSwitchRequest({ request: { ...request, status: "succeeded" } })).toBeNull();
    expect(activeSwitchRequest({ request: { ...request, status: "failed" } })).toBeNull();
  });
});

describe("Line Switch parallel ports", () => {
  it("builds the target line URL from the current host and target backend port", () => {
    expect(
      redirectUrlForLine(
        { line_targets: { qa: { backend_port: 18767 } } },
        "qa",
        "http://192.168.1.10:18765/session/s1?x=1#bottom",
      ),
    ).toBe("http://192.168.1.10:18767/session/s1?x=1#bottom");
  });

  it("refuses invalid target port metadata", () => {
    expect(
      redirectUrlForLine(
        { line_targets: { main: { backend_port: "not-a-port" } } },
        "main",
        "http://127.0.0.1:18765/",
      ),
    ).toBe("");
  });

  it("uses the backend-provided target URL when present", () => {
    expect(switchTargetUrl({ target_url: "http://127.0.0.1:18766/" })).toBe(
      "http://127.0.0.1:18766/",
    );
    expect(switchTargetUrl({ target_url: "" })).toBe("");
  });
});
