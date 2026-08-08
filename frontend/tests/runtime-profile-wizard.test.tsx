import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import "../src/i18n";
import { SettingsPage } from "../src/components/SettingsPage";
import { toRuntimeProfilesSnapshotEnvelope } from "./fixtures";
import type { RuntimeProfilesSnapshot } from "../src/types";

// Locks the C2 creation wizard: provider (existing account) → runner cards
// derived from the backend's mode-aware runner_options (a pair with a live
// profile is offered as taken/disabled) → defaults (name prefilled
// "Provider (runner)", model from the catalog) → POST /api/runtime-profiles.
// The duplicate-pair 400 surfaces as an inline error, not a crash.

function response(body: unknown, ok = true, status = 200) {
  return Promise.resolve({
    ok,
    status,
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(typeof body === "string" ? body : JSON.stringify(body)),
  } as Response);
}

const claudeProvider = {
  id: "claude-native",
  generation: "generation-1",
  revision: 1,
  name: "Claude",
  kind: "claude",
  mode: "subscription",
  base_url: "",
  config_dir: "",
  custom_models: [],
  runner_options: ["native", "better_agent_runner"],
  runner_profiles: [
    { runner: "native", reasoning_efforts: ["low", "medium", "high", "xhigh"] },
    { runner: "better_agent_runner", reasoning_efforts: ["none", "minimal", "low", "medium", "high", "xhigh"] },
  ],
  suspended: false,
  reasoning_effort_options: ["low", "medium", "high", "xhigh"],
  permission_options: {},
  default_permission: {},
  has_api_key: false,
  credential_status: "available",
  capability_overrides: {},
  supports_fork: true,
  supports_manager_mode: true,
  supports_rewind: true,
  supports_steering: false,
  supports_native_subagents: true,
  supports_reasoning_effort: true,
};

const snapshot = {
  runtime_profiles: [
    {
      id: "rp-bar",
      provider_id: "claude-native",
      runner: "better_agent_runner",
      name: "Claude (Better Agent)",
      default_model: "claude-opus-5[1m]",
      default_reasoning_effort: "medium",
      created_at: "2026-07-30T00:00:00Z",
      updated_at: "2026-07-30T00:00:00Z",
      deleted_at: null,
    },
  ],
  default_runtime_profile_id: "rp-bar",
  deleted_providers: [],
  last_models: {},
  last_reasoning_efforts: {},
} satisfies RuntimeProfilesSnapshot;

function mockFetch({ createStatus = 200, createBody = null as unknown }: {
  createStatus?: number;
  createBody?: unknown;
} = {}) {
  return vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
    const url = String(input);
    if (url.match(/\/api\/providers\/[^/]+\/models$/)) {
      return response({
        provider_id: "claude-native",
        provider_generation: "generation-1",
        authoritative: false,
        status: "current",
        models: ["claude-opus-5[1m]", "claude-haiku-4-5"],
        models_current: true,
        retired: [],
        retired_models: [],
        last_refreshed_at: 1,
        reason: "",
        authority_fingerprint: "",
        last_known_good: null,
        runtime_profiles: [],
      });
    }
    if (url.includes("/api/v2/surface/runtime-profiles")) {
      return response(toRuntimeProfilesSnapshotEnvelope(snapshot));
    }
    if (url.includes("/api/runtime-profiles")) {
      if (init?.method === "POST") {
        if (createStatus !== 200) {
          return response(createBody ?? "duplicate", false, createStatus);
        }
        return response({
          id: "rp-new",
          provider_id: "claude-native",
          runner: "native",
          name: "Claude (Claude)",
          default_model: "claude-opus-5[1m]",
          default_reasoning_effort: "medium",
          created_at: "2026-07-30T01:00:00Z",
          updated_at: "2026-07-30T01:00:00Z",
          deleted_at: null,
        });
      }
      return response(snapshot);
    }
    if (url.includes("/api/providers")) {
      return response({ providers: [claudeProvider], default_provider_id: "claude-native" });
    }
    if (url.includes("/api/provider-setup/status")) return response({ providers: [] });
    if (url.includes("/api/user-prefs")) {
      return response({ first_run_wizard_done: true, network_bind_address: "127.0.0.1" });
    }
    if (url.includes("/api/projects")) return response({ projects: [] });
    if (url.includes("/api/installation-profile")) return response({});
    return Promise.resolve({ ok: false, status: 404, text: () => Promise.resolve("") } as Response);
  });
}

async function walkToDefaults() {
  // Deep link straight into the wizard (the NewSessionModal affordance URL).
  window.history.pushState(null, "", "/settings?section=runtimeProfiles&createProfile=1");
  render(<SettingsPage onClose={() => {}} hookActionContext={{}} />);

  // Step 1: existing provider card.
  const card = await screen.findByRole("button", { name: /Claude/ });
  fireEvent.click(card);
  fireEvent.click(screen.getByRole("button", { name: "Continue" }));

  // Step 2: runner cards from runner_options — the pair with a live profile
  // (better_agent_runner) is disabled as taken.
  const takenCard = await screen.findByTestId("rpw-runner-better_agent_runner");
  expect((takenCard as HTMLButtonElement).disabled).toBe(true);
  const nativeCard = screen.getByTestId("rpw-runner-native");
  expect((nativeCard as HTMLButtonElement).disabled).toBe(false);
  fireEvent.click(nativeCard);

  // Step 3: name prefilled "Provider (runner label)".
  const nameInput = await screen.findByDisplayValue("Claude (Claude)");
  return nameInput;
}

describe("runtime-profile creation wizard", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    localStorage.clear();
  });

  it("creates a profile: provider → free runner → defaults → POST", async () => {
    const fetchMock = mockFetch();
    await walkToDefaults();

    fireEvent.click(await screen.findByTestId("rpw-create-profile"));

    await waitFor(() => {
      const post = fetchMock.mock.calls.find(([url, init]) => (
        String(url).endsWith("/api/runtime-profiles") && init?.method === "POST"
      ));
      expect(post).toBeTruthy();
      const body = JSON.parse(String(post?.[1]?.body));
      expect(body.provider_id).toBe("claude-native");
      expect(body.runner).toBe("native");
      expect(body.name).toBe("Claude (Claude)");
      expect(body.default_model).toBe("claude-opus-5[1m]");
      expect(body.default_reasoning_effort).toBe("medium");
    });
  });

  it("surfaces the duplicate-pair 400 inline", async () => {
    mockFetch({
      createStatus: 400,
      createBody: "a live runtime profile already exists for provider claude-native with runner native",
    });
    await walkToDefaults();

    fireEvent.click(await screen.findByTestId("rpw-create-profile"));

    await waitFor(() => {
      expect(
        screen.getByText("A live profile for this provider and runner already exists."),
      ).toBeTruthy();
    });
  });
});
