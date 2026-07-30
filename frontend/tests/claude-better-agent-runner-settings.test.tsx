import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import "../src/i18n";
import { SettingsPage } from "../src/components/SettingsPage";

// Locks that the Settings provider-edit form now offers claude's
// better_agent_runner runner choice, that picking it hides the api_key
// mode toggle entirely (mode locks to subscription — the backend has no
// api_key backend for this combination), and that this is NOT granted to
// openai/fugu beyond what already existed.

function response(body: unknown) {
  return Promise.resolve({
    ok: true,
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(""),
  } as Response);
}

const claudeProvider = {
  id: "claude-native",
  name: "Claude",
  kind: "claude",
  mode: "subscription",
  base_url: "",
  config_dir: "",
  custom_models: [],
  default_model: "claude-opus-4-6",
  runner: "native",
  runner_options: ["native", "better_agent_runner"],
  suspended: false,
  reasoning_effort_options: ["low", "medium", "high", "xhigh"],
  default_reasoning_effort: "medium",
  permission_options: {},
  default_permission: {},
  has_api_key: false,
  credential_status: "available",
  supports_fork: true,
  supports_manager_mode: true,
  supports_rewind: true,
  supports_steering: false,
  supports_native_subagents: true,
  supports_reasoning_effort: true,
};

const openaiProvider = {
  id: "openai-provider",
  name: "OpenAI-compatible",
  kind: "openai",
  mode: "api_key",
  base_url: "https://api.example.invalid/v1",
  config_dir: "",
  custom_models: [],
  default_model: "gpt-test",
  runner: "better_agent_runner",
  runner_options: ["better_agent_runner"],
  suspended: false,
  reasoning_effort_options: [],
  default_reasoning_effort: "",
  permission_options: {},
  default_permission: {},
  has_api_key: true,
  credential_status: "available",
  supports_fork: true,
  supports_manager_mode: true,
  supports_rewind: true,
  supports_steering: true,
  supports_native_subagents: true,
  supports_reasoning_effort: true,
};

function mockFetch(providers: unknown[]) {
  return vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
    const url = String(input);
    if (url.includes("/api/providers") && url.match(/\/models$/)) {
      return response({
        provider_id: "claude-native",
        provider_generation: "generation-1",
        authoritative: false,
        status: "current",
        models: [],
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
    if (url.includes("/api/providers")) {
      return response({ providers, default_provider_id: providers[0] ? (providers[0] as { id: string }).id : null });
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

describe("claude better_agent_runner settings form", () => {
  afterEach(() => vi.restoreAllMocks());

  it("offers better_agent_runner for claude, hides api_key mode once selected, and saves subscription+better_agent_runner", async () => {
    const fetchMock = mockFetch([claudeProvider]);

    render(<SettingsPage onClose={() => {}} />);

    const row = await screen.findByTestId("provider-row-claude");
    fireEvent.click(row.querySelector(".provider-row-main")!);

    // Both mode buttons are visible while runner is "native".
    expect(await screen.findByRole("button", { name: "Subscription" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "API Key" })).toBeTruthy();

    // "Runner" is a plain sibling <label>, not a for/id-associated one, so
    // getByLabelText can't resolve it — walk from the label text instead.
    const runnerSelect = screen.getByText("Runner").closest(".setup-field")!
      .querySelector("select") as HTMLSelectElement;
    const runnerValues = Array.from(runnerSelect.options).map((o) => o.value);
    expect(runnerValues).toEqual(["native", "better_agent_runner"]);

    fireEvent.change(runnerSelect, { target: { value: "better_agent_runner" } });

    // Selecting better_agent_runner hides the api_key mode toggle
    // entirely — there is no api_key backend for this combination — and
    // the form is left on subscription (no forced-away-from-user-choice
    // behavior the way fugu forces api_key).
    await waitFor(() => {
      expect(screen.queryByRole("button", { name: "API Key" })).toBeNull();
    });
    expect(screen.queryByRole("button", { name: "Subscription" })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));

    await waitFor(() => {
      const patch = fetchMock.mock.calls.find(([url, init]) => (
        String(url).endsWith("/api/providers/claude-native") && init?.method === "PATCH"
      ));
      expect(patch).toBeTruthy();
      const body = JSON.parse(String(patch?.[1]?.body));
      expect(body.runner).toBe("better_agent_runner");
      expect(body.mode).toBe("subscription");
    });
  });

  it("does not grant the better_agent_runner mode-hiding behavior to openai", async () => {
    mockFetch([openaiProvider]);

    render(<SettingsPage onClose={() => {}} />);

    const row = await screen.findByTestId("provider-row-openai");
    fireEvent.click(row.querySelector(".provider-row-main")!);

    // openai has only one runner choice — no runner <select> is rendered
    // at all (runnerOptions.length is not > 1).
    expect(screen.queryByText("Runner")).toBeNull();
    // openai is api_key-only — the mode toggle is likewise not rendered
    // (modes.length is not > 1), same as before this change.
    expect(screen.queryByRole("button", { name: "Subscription" })).toBeNull();
    expect(screen.queryByRole("button", { name: "API Key" })).toBeNull();
  });
});
