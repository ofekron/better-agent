// @vitest-environment happy-dom
//
// B6 ruling (parity audit): `useRuntimeProfiles()`'s v2 cutover had no
// legacy REST fallback, unlike every sibling provider-plane hook
// (`useProviderChanged`/`useModelsCatalogChanged`/`useProviderInstalls`
// all keep a legacy path reachable). This proves `refresh()` falls back to
// the legacy `fetchRuntimeProfiles()` REST helper when the v2 fetch fails,
// giving this hook the same resilience its siblings already have.

import { afterEach, describe, expect, it, vi } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { useRuntimeProfiles } from "../src/hooks/useRuntimeProfiles";
import { SurfaceClient } from "../src/adapter/client";
import * as api from "../src/api";
import type { RuntimeProfilesSnapshot } from "../src/types";

vi.mock("../src/hooks/useProviderSurfaceSocket", () => ({
  subscribeProviderFrames: () => () => {},
  subscribeProviderSocketConnection: () => () => {},
  isProviderSocketOpen: () => false,
}));

const v2Snapshot = {
  profiles: [{
    runtime_profile_id: "rp1", provider_id: "p1", runner: "better_agent_runner", name: "Default",
    default_model: "opus", default_reasoning_effort: "medium",
    created_at: 1, updated_at: 1, deleted_at: null,
  }],
  default_runtime_profile_id: "rp1",
  deleted_providers: [],
  last_models: {},
  last_reasoning_efforts: {},
};

const legacySnapshot: RuntimeProfilesSnapshot = {
  runtime_profiles: [{
    id: "rp-legacy", provider_id: "p1", runner: "better_agent_runner", name: "Legacy Default",
    default_model: "sonnet", default_reasoning_effort: "medium",
    created_at: 1, updated_at: 1, deleted_at: null,
  }],
  default_runtime_profile_id: "rp-legacy",
  deleted_providers: [],
  last_models: {},
  last_reasoning_efforts: {},
};

afterEach(() => {
  vi.restoreAllMocks();
  localStorage.clear();
});

describe("useRuntimeProfiles — legacy REST fallback (B6)", () => {
  it("uses the v2 snapshot when the v2 fetch succeeds (no fallback call)", async () => {
    const v2Fetch = vi.spyOn(SurfaceClient.prototype, "fetchRuntimeProfileDescriptors")
      .mockResolvedValue(v2Snapshot as never);
    const legacyFetch = vi.spyOn(api, "fetchRuntimeProfiles").mockResolvedValue(legacySnapshot);

    const { result } = renderHook(() => useRuntimeProfiles());
    await waitFor(() => expect(result.current.loading).toBe(false));

    expect(result.current.profiles).toHaveLength(1);
    expect(result.current.profiles[0].name).toBe("Default");
    expect(result.current.error).toBeNull();
    expect(v2Fetch).toHaveBeenCalledTimes(1);
    expect(legacyFetch).not.toHaveBeenCalled();
  });

  it("falls back to the legacy REST route when the v2 fetch fails, and still resolves cleanly", async () => {
    const v2Fetch = vi.spyOn(SurfaceClient.prototype, "fetchRuntimeProfileDescriptors")
      .mockRejectedValue(new Error("HTTP 500: v2 down"));
    const legacyFetch = vi.spyOn(api, "fetchRuntimeProfiles").mockResolvedValue(legacySnapshot);

    const { result } = renderHook(() => useRuntimeProfiles());
    await waitFor(() => expect(result.current.loading).toBe(false));

    expect(result.current.error).toBeNull();
    expect(result.current.profiles).toHaveLength(1);
    expect(result.current.profiles[0].name).toBe("Legacy Default");
    expect(v2Fetch).toHaveBeenCalledTimes(1);
    expect(legacyFetch).toHaveBeenCalledTimes(1);
  });

  it("surfaces an error only when BOTH the v2 fetch and the legacy fallback fail", async () => {
    vi.spyOn(SurfaceClient.prototype, "fetchRuntimeProfileDescriptors")
      .mockRejectedValue(new Error("HTTP 500: v2 down"));
    vi.spyOn(api, "fetchRuntimeProfiles").mockRejectedValue(new Error("HTTP 503: legacy down too"));

    const { result } = renderHook(() => useRuntimeProfiles());
    await waitFor(() => expect(result.current.loading).toBe(false));

    expect(result.current.error).toBe("HTTP 503: legacy down too");
    expect(result.current.profiles).toEqual([]);
  });
});
