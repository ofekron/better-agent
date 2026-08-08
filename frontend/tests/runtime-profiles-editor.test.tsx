import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import "../src/i18n";
import { SettingsPage } from "../src/components/SettingsPage";
import { toRuntimeProfilesSnapshotEnvelope } from "./fixtures";
import type { RuntimeProfilesSnapshot } from "../src/types";

// Locks the C3 management section: profile rail with default + tombstone
// badges, name/defaults edits via PATCH, Activate via the profile activate
// route (the only default-selection surface), soft-delete via DELETE with
// confirm, and tombstoned-profile provider display through deleted_providers.

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

const profileDefault = {
  id: "rp-default",
  provider_id: "claude-native",
  runner: "better_agent_runner",
  name: "Claude (Better Agent)",
  default_model: "claude-opus-5[1m]",
  default_reasoning_effort: "medium",
  created_at: "2026-07-30T00:00:00Z",
  updated_at: "2026-07-30T00:00:00Z",
  deleted_at: null,
};
const profileLive = {
  id: "rp-live",
  provider_id: "claude-native",
  runner: "native",
  name: "Claude (Claude)",
  default_model: "claude-opus-5[1m]",
  default_reasoning_effort: "medium",
  created_at: "2026-07-30T00:00:00Z",
  updated_at: "2026-07-30T00:00:00Z",
  deleted_at: null,
};
const profileTombstone = {
  id: "rp-dead",
  provider_id: "gone-provider",
  runner: "native",
  name: "Old Codex",
  default_model: "gpt-5.5",
  default_reasoning_effort: "",
  created_at: "2026-07-01T00:00:00Z",
  updated_at: "2026-07-02T00:00:00Z",
  deleted_at: "2026-07-03T00:00:00Z",
};

const snapshot = {
  runtime_profiles: [profileDefault, profileLive, profileTombstone],
  default_runtime_profile_id: "rp-default",
  deleted_providers: [
    { id: "gone-provider", name: "Codex (old)", kind: "codex", deleted_at: "2026-07-03T00:00:00Z" },
  ],
  last_models: {},
  last_reasoning_efforts: {},
} satisfies RuntimeProfilesSnapshot;

function mockFetch() {
  return vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
    const url = String(input);
    const method = init?.method ?? "GET";
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
    if (url.match(/\/api\/runtime-profiles\/[^/]+\/activate$/) && method === "POST") {
      return response({ ...profileLive });
    }
    if (url.match(/\/api\/runtime-profiles\/[^/]+$/) && method === "PATCH") {
      return response({ ...profileLive, name: "Renamed" });
    }
    if (url.match(/\/api\/runtime-profiles\/[^/]+$/) && method === "DELETE") {
      return response({ deleted: true });
    }
    if (url.includes("/api/v2/surface/runtime-profiles")) {
      return response(toRuntimeProfilesSnapshotEnvelope(snapshot));
    }
    if (url.includes("/api/runtime-profiles")) return response(snapshot);
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

async function renderEditor() {
  window.history.pushState(null, "", "/settings?section=runtimeProfiles");
  render(<SettingsPage onClose={() => {}} hookActionContext={{}} />);
  await screen.findByTestId("rpe-rail-rp-default");
}

describe("runtime-profiles management section", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    localStorage.clear();
  });

  it("renders the rail with default badge and greyed tombstone with deleted badge", async () => {
    mockFetch();
    await renderEditor();

    const defaultItem = screen.getByTestId("rpe-rail-rp-default");
    expect(defaultItem.textContent).toContain("Default");

    const deadItem = screen.getByTestId("rpe-rail-rp-dead");
    expect(deadItem.className).toContain("is-deleted");
    expect(deadItem.textContent).toContain("Deleted");
  });

  it("shows the tombstoned profile's provider from deleted_providers", async () => {
    mockFetch();
    await renderEditor();

    fireEvent.click(screen.getByTestId("rpe-rail-rp-dead"));
    await waitFor(() => {
      expect(screen.getByText(/Codex \(old\)/)).toBeTruthy();
    });
  });

  it("saves name edits via PATCH", async () => {
    const fetchMock = mockFetch();
    await renderEditor();

    fireEvent.click(screen.getByTestId("rpe-rail-rp-live"));
    const nameInput = await screen.findByTestId("rpe-name-input");
    fireEvent.change(nameInput, { target: { value: "Renamed" } });
    fireEvent.click(screen.getByTestId("rpe-save"));

    await waitFor(() => {
      const patch = fetchMock.mock.calls.find(([url, init]) => (
        String(url).endsWith("/api/runtime-profiles/rp-live") && init?.method === "PATCH"
      ));
      expect(patch).toBeTruthy();
      const body = JSON.parse(String(patch?.[1]?.body));
      expect(body.name).toBe("Renamed");
    });
  });

  it("activates a non-default profile through the activate route", async () => {
    const fetchMock = mockFetch();
    await renderEditor();

    fireEvent.click(screen.getByTestId("rpe-rail-rp-live"));
    fireEvent.click(await screen.findByTestId("rpe-activate"));

    await waitFor(() => {
      const post = fetchMock.mock.calls.find(([url, init]) => (
        String(url).endsWith("/api/runtime-profiles/rp-live/activate") && init?.method === "POST"
      ));
      expect(post).toBeTruthy();
    });
  });

  it("soft-deletes after confirm; the default profile offers no delete", async () => {
    const fetchMock = mockFetch();
    vi.stubGlobal("confirm", vi.fn(() => true));
    await renderEditor();

    // Default profile pane: no delete/activate actions.
    expect(screen.queryByTestId("rpe-delete")).toBeNull();
    expect(screen.queryByTestId("rpe-activate")).toBeNull();

    fireEvent.click(screen.getByTestId("rpe-rail-rp-live"));
    fireEvent.click(await screen.findByTestId("rpe-delete"));

    await waitFor(() => {
      const del = fetchMock.mock.calls.find(([url, init]) => (
        String(url).endsWith("/api/runtime-profiles/rp-live") && init?.method === "DELETE"
      ));
      expect(del).toBeTruthy();
    });
    vi.unstubAllGlobals();
  });
});
