import { describe, expect, it, vi } from "vitest";
import { createBetterAgentProviderConfigSyncClient } from "@better-agent/provider-config-sync-ui";
import { openProviderConfigSyncPage, providerConfigSyncUrl, PROVIDER_CONFIG_SYNC_PATH } from "../src/lib/providerConfigSyncRoute";

describe("provider config sync route", () => {
  it("opens the provider config sync page in a new tab", () => {
    const open = vi.spyOn(window, "open").mockImplementation(() => null);

    openProviderConfigSyncPage("http://localhost:8000");

    expect(open).toHaveBeenCalledWith("http://localhost:8000/provider-config-sync", "_blank", "noopener,noreferrer");
  });

  it("uses the consumer-provided base URL", () => {
    expect(providerConfigSyncUrl("http://localhost:8000")).toBe("http://localhost:8000/provider-config-sync");
    expect(providerConfigSyncUrl("http://localhost:8000/")).toBe("http://localhost:8000/provider-config-sync");
  });

  it("uses the same-origin route without a base URL", () => {
    expect(providerConfigSyncUrl()).toBe(PROVIDER_CONFIG_SYNC_PATH);
  });

  it("uses Better Agent routes for the capability picker client", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ sources: [] }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    const client = createBetterAgentProviderConfigSyncClient({ baseUrl: "", credentials: "include" });

    await client.listCapabilityPickerSources("/tmp/project");

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/provider-config-sync/capability-picker?cwd=%2Ftmp%2Fproject",
      expect.any(Object),
    );
  });
});
