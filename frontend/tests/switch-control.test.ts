import { describe, expect, it } from "vitest";

import {
  activeSwitchRequest,
  restartStatusForRequest,
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
