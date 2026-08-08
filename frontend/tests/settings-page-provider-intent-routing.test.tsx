// D4 intent-error-propagation + mutation-call-site wiring (closure 1):
// `SettingsPage.tsx`'s provider mutation call sites (create/update/delete/
// suspend/resume/credential-retry/login/cancel-login) route through
// `submitProviderIntent` when the v2 `/ws/v2/surface` "providers" feed
// socket is open (native-when-open/legacy-fallback gating, established by
// `useProviderChanged`'s existing dual subscription — SettingsPage always
// mounts it, which lazily opens the shared socket). This file proves: (1)
// a mutation goes out as a WS intent instead of a REST call once the
// socket is open, and (2) a `intent_rejected` frame — synchronous OR the
// async D4 echo — surfaces its real message in the existing error UI.

import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import "../src/i18n";
import { SettingsPage } from "../src/components/SettingsPage";
import { MockWebSocketController } from "./harness/mockWebSocket";

function response(body: unknown) {
  return Promise.resolve({
    ok: true,
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(""),
  } as Response);
}

const provider = {
  id: "provider-1",
  name: "Claude",
  kind: "claude",
  mode: "subscription",
  base_url: "",
  config_dir: "",
  custom_models: [],
  default_model: "model",
  runner: "better_agent_runner",
  runner_options: ["better_agent_runner"],
  suspended: false,
  reasoning_effort_options: [],
  default_reasoning_effort: "",
  permission_options: {},
  default_permission: {},
  has_api_key: false,
  generation: "gen-1",
  revision: 1,
  supports_fork: false,
  supports_manager_mode: false,
  supports_rewind: false,
  supports_steering: false,
  supports_native_subagents: false,
  supports_reasoning_effort: false,
};

let ws: MockWebSocketController;

function mockFetch(): ReturnType<typeof vi.spyOn> {
  return vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
    const url = String(input);
    if (url.includes("/api/providers/provider-1/models")) {
      return response({
        provider_id: provider.id, provider_generation: "gen-1", authoritative: false,
        status: "current", models: [], models_current: true, retired: [], retired_models: [],
        last_refreshed_at: 1, reason: "", authority_fingerprint: "", last_known_good: null,
        runtime_profiles: [],
      });
    }
    if (url.includes("/api/providers")) {
      return response({ providers: [provider], default_provider_id: provider.id });
    }
    if (url.includes("/api/provider-setup/status")) return response({ providers: [] });
    if (url.includes("/api/user-prefs")) {
      return response({ first_run_wizard_done: true, network_bind_address: "127.0.0.1" });
    }
    if (url.includes("/api/projects")) return response({ projects: [] });
    if (url.includes("/repository")) return response({ configured: false });
    if (url.includes("/api/settings/password-manager")) return response({ items: [] });
    return Promise.resolve({ ok: false, status: 404, text: () => Promise.resolve("") } as Response);
  });
}

describe("SettingsPage provider mutations — native intent routing", () => {
  beforeEach(() => {
    ws = new MockWebSocketController();
    ws.install();
  });
  afterEach(() => {
    ws.uninstall();
    vi.restoreAllMocks();
  });

  it("suspends via a WS intent (not REST) once the providers socket is open, and shows the real async rejection message", async () => {
    const fetchMock = mockFetch();
    const { unmount } = render(<SettingsPage onClose={() => {}} />);

    await screen.findByText("Claude");
    // `useProviderChanged` (always mounted by SettingsPage) lazily opens
    // the shared "providers" feed socket — wait for it before asserting
    // native routing, exactly like `useProviderSurfaceSocket.test.ts` does.
    await vi.waitFor(() => {
      expect(
        ws.outbound.some(
          (f) => Array.isArray((f as { feeds?: unknown[] }).feeds)
            && (f as { feeds: unknown[] }).feeds.includes("providers"),
        ),
      ).toBe(true);
    });

    fireEvent.click(screen.getByRole("button", { name: "Suspend" }));

    const sentIntent = await vi.waitFor(() => {
      const found = ws.outbound.find(
        (f) => (f as { intent?: { kind?: string } }).intent?.kind === "suspend_provider",
      ) as { intent: { intent_id: string; provider_id: string } } | undefined;
      if (!found) throw new Error("suspend_provider intent not sent yet");
      return found;
    });
    expect(sentIntent.intent.provider_id).toBe("provider-1");

    // Native path taken — the legacy REST `/suspended` mutation must NOT
    // have fired.
    expect(fetchMock.mock.calls.some(([url]) => String(url).endsWith("/suspended"))).toBe(false);

    ws.emit({ type: "intent_accepted", intent_id: sentIntent.intent.intent_id } as never);
    await Promise.resolve();
    ws.emit({
      type: "intent_rejected", intent_id: sentIntent.intent.intent_id,
      code: "409", message: "provider was modified concurrently",
    } as never);

    expect(await screen.findByText("provider was modified concurrently")).toBeTruthy();
    unmount();
  });
});
