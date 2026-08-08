// A9 (parity audit): `EditProvider` must gate ProviderForm rendering on the
// installable-catalog fetch's own loading/error state rather than an empty
// `formSchema` — otherwise editing an OpenAI-kind provider while the
// catalog is still loading (or failed) silently falls back to the GENERIC
// ANTHROPIC_API_KEY label instead of the openai-flavored OPENAI_API_KEY
// one, and the mode toggle / config-dir field vanish. Verified end-to-end
// through a real `SettingsPage` render (not just the isolated component),
// same harness `settings-page-provider-intent-routing.test.tsx` uses.

import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import "../src/i18n";
import { SettingsPage } from "../src/components/SettingsPage";
import { MockWebSocketController } from "./harness/mockWebSocket";

function response(body: unknown, ok = true, status = ok ? 200 : 500) {
  return Promise.resolve({
    ok,
    status,
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(typeof body === "string" ? body : ""),
  } as Response);
}

const provider = {
  id: "provider-1",
  name: "OpenAI Compatible",
  kind: "openai",
  mode: "api_key",
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
let resolveInstallable: (v: Response) => void = () => {};
let installablePending: Promise<Response>;

function armInstallablePending() {
  installablePending = new Promise<Response>((resolve) => {
    resolveInstallable = resolve;
  });
}

function mockFetch(): void {
  vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
    const url = String(input);
    if (url.includes("/api/v2/surface/providers/installable")) return installablePending;
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
    return response("", false, 404);
  });
}

function installableDescriptor(kind: string) {
  return {
    kind,
    display: { label: kind, icon_id: kind, config_copy_key: `provider.config_copy.${kind}` },
    form_schema: [
      { name: "api_key", kind: "secret", label_key: "setup.apiKeyLabelOpenai", required: false, choices: [], default: null, pattern: null, max_length: null, placeholder_key: "setup.apiKeyPlaceholderEmptyOpenai", hint_key: null },
      { name: "base_url", kind: "text", label_key: "setup.baseUrlLabelOpenai", required: false, choices: [], default: null, pattern: null, max_length: null, placeholder_key: null, hint_key: null },
    ],
    defaults: { name: kind, kind, mode: "api_key", base_url: "", config_dir: "", default_model: "", default_reasoning_effort: "" },
    auth_flows: ["api_key"],
  };
}

describe("SettingsPage edit provider — installable-catalog loading/error gate (A9)", () => {
  beforeEach(() => {
    ws = new MockWebSocketController();
    ws.install();
    armInstallablePending();
  });
  afterEach(() => {
    ws.uninstall();
    vi.restoreAllMocks();
  });

  it("shows a loading placeholder (never the generic wrong-kind label) while the catalog is in flight", async () => {
    mockFetch();
    render(<SettingsPage onClose={() => {}} />);

    await screen.findByText("OpenAI Compatible");
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));

    expect(await screen.findByText("Edit provider")).toBeTruthy();
    expect(screen.getByText("Loading…")).toBeTruthy();
    expect(screen.queryByText("ANTHROPIC_API_KEY")).toBeNull();
    expect(screen.queryByText("OPENAI_API_KEY")).toBeNull();
  });

  it("shows an error + retry (never a blank/wrong-kind form) when the catalog fetch fails, and recovers on retry", async () => {
    mockFetch();
    render(<SettingsPage onClose={() => {}} />);

    await screen.findByText("OpenAI Compatible");
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    await screen.findByText("Loading…");

    resolveInstallable(await response("server exploded", false, 500));
    expect(await screen.findByTestId("provider-catalog-error")).toBeTruthy();
    expect(screen.getByText(/HTTP 500/)).toBeTruthy();
    expect(screen.queryByText("ANTHROPIC_API_KEY")).toBeNull();

    armInstallablePending();
    fireEvent.click(screen.getByTestId("provider-catalog-error").querySelector("button")!);
    resolveInstallable(await response({ value: [installableDescriptor("openai")] }));

    // Once resolved, the real form renders with the OPENAI-flavored label —
    // never the generic ANTHROPIC_API_KEY fallback a wrong-kind gate would show.
    expect(await screen.findByText("OPENAI_API_KEY")).toBeTruthy();
    expect(screen.queryByText("ANTHROPIC_API_KEY")).toBeNull();
  });
});
