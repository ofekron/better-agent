import { describe, it, expect } from "vitest";
import { waitFor } from "@testing-library/react";
import { renderApp } from "./harness";
import { parseProvidersPayload } from "../src/utils/providerCache";

/** Regression lock for the "Something went wrong / Cannot read properties of
 * undefined (reading 'find')" app-blanking crash.
 *
 * Pre-fix root cause: the installation admission gate 503s /api/providers
 * with {detail: "installation setup is required"}; App.tsx's syncProvider
 * parsed that body without an r.ok check and wrote `undefined` into the
 * global `providers` state, crashing every providers.find consumer
 * (App defaultProvider memo, SessionNode, SessionTabs) into the top-level
 * ErrorBoundary.
 */
describe("installation setup gate", () => {
  it("shows a setup-required banner instead of crashing when /api/providers is gated", async () => {
    const h = await renderApp({
      seed: {
        installationProfile: {
          status: "setup_required",
          setup_required: true,
          mode: null,
          provider_conversations_enabled: false,
          mobile_enabled: false,
          integrations_enabled: false,
        },
      },
    });
    await h.flush();

    const crashed = h
      .$$("h1")
      .some((el) => el.textContent?.includes("Something went wrong"));
    expect(crashed).toBe(false);

    await waitFor(() => {
      const banner = h.$(".offline-banner--warn");
      expect(banner?.textContent).toContain("Installation setup is required");
    });

    h.unmount();
  });

  it("keeps working provider state when the gate opens mid-session", async () => {
    const h = await renderApp({});
    await h.flush();

    h.backend.state.installationProfile = {
      status: "setup_required",
      setup_required: true,
      mode: null,
      provider_conversations_enabled: false,
      mobile_enabled: false,
      integrations_enabled: false,
    };
    h.emit({ type: "provider_changed" });
    await h.flush();

    const crashed = h
      .$$("h1")
      .some((el) => el.textContent?.includes("Something went wrong"));
    expect(crashed).toBe(false);

    await waitFor(() => {
      expect(h.$(".offline-banner--warn")?.textContent).toContain(
        "Installation setup is required",
      );
    });

    h.unmount();
  });

  it("rejects error envelopes and malformed bodies at the providers boundary", () => {
    expect(parseProvidersPayload({ detail: "installation setup is required" })).toBeNull();
    expect(parseProvidersPayload(undefined)).toBeNull();
    expect(parseProvidersPayload(null)).toBeNull();
    expect(parseProvidersPayload([])).toBeNull();
    expect(
      parseProvidersPayload({ providers: undefined, default_provider_id: null }),
    ).toBeNull();
    expect(
      parseProvidersPayload({ providers: [{ id: "x" }], default_provider_id: null }),
    ).toBeNull();
  });
});
