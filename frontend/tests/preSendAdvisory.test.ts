import { afterEach, describe, expect, it, vi } from "vitest";
import {
  cachedPreSendAdvisories,
  clearPreSendAdvisoryCacheForTests,
  refreshPreSendAdvisories,
} from "../src/utils/preSendAdvisory";

describe("pre-send advisory cache", () => {
  afterEach(() => {
    clearPreSendAdvisoryCacheForTests();
    vi.restoreAllMocks();
  });

  it("refreshes advisories without making send wait for the fetch", async () => {
    let resolveFetch: ((value: Response) => void) | undefined;
    const fetchPromise = new Promise<Response>((resolve) => {
      resolveFetch = resolve;
    });
    const fetchMock = vi.spyOn(globalThis, "fetch").mockReturnValue(fetchPromise as Promise<Response>);

    refreshPreSendAdvisories("http://api", "sid", "provider", "model");

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(cachedPreSendAdvisories("http://api", "sid", "provider", "model")).toBeNull();

    resolveFetch?.(
      new Response(
        JSON.stringify({
          advisories: [{ extension_id: "usage", title: "Low quota", severity: "warn" }],
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );
    await fetchPromise;

    await expect
      .poll(() => cachedPreSendAdvisories("http://api", "sid", "provider", "model"))
      .toEqual([{ extension_id: "usage", title: "Low quota", severity: "warn" }]);
  });

  it("deduplicates concurrent refreshes for the same advisory key", () => {
    vi.spyOn(globalThis, "fetch").mockReturnValue(new Promise<Response>(() => {}));

    refreshPreSendAdvisories("http://api", "sid", "provider", "model");
    refreshPreSendAdvisories("http://api", "sid", "provider", "model");

    expect(globalThis.fetch).toHaveBeenCalledTimes(1);
  });
});
