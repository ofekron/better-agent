import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import "../src/i18n";
import { SettingsPage } from "../src/components/SettingsPage";

function response(body: unknown) {
  return Promise.resolve({
    ok: true,
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(""),
  } as Response);
}

describe("provider credential denial", () => {
  afterEach(() => vi.restoreAllMocks());

  it("keeps retry and explicit key re-entry as separate blocked-state actions", async () => {
    let resolveRetry: ((value: Response) => void) | undefined;
    const retryResponse = new Promise<Response>((resolve) => {
      resolveRetry = resolve;
    });
    const provider = {
      id: "provider-blocked",
      name: "Blocked provider",
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
      credential_status: "blocked",
      supports_fork: false,
      supports_manager_mode: false,
      supports_rewind: false,
      supports_steering: false,
      supports_native_subagents: false,
      supports_reasoning_effort: false,
    };
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      const url = String(input);
      if (url.endsWith("/credential/retry")) {
        return retryResponse;
      }
      if (url.endsWith("/api/providers/provider-blocked/models")) {
        return response({ models: [] });
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

    const { unmount } = render(
      <SettingsPage onClose={() => {}} onOpenProviderConfigSync={() => {}} />,
    );

    expect(await screen.findByText(/access blocked/)).toBeTruthy();
    expect(fetchMock.mock.calls.some(([url, init]) => (
      String(url).endsWith("/credential/retry") && init?.method === "POST"
    ))).toBe(false);

    fireEvent.click(screen.getByRole("button", { name: "Retry" }));

    const waitingButton = await screen.findByRole("button", {
      name: "Waiting for credential access…",
    });
    expect((waitingButton as HTMLButtonElement).disabled).toBe(true);

    await waitFor(() => {
      expect(fetchMock.mock.calls.filter(([url, init]) => (
        String(url).endsWith("/credential/retry") && init?.method === "POST"
      ))).toHaveLength(1);
    });

    resolveRetry?.({
      ok: true,
      json: () => Promise.resolve({ credential_status: "available", has_api_key: true }),
      text: () => Promise.resolve(""),
    } as Response);

    await screen.findByRole("button", { name: "Retry" });

    fireEvent.click(screen.getByRole("button", { name: "Re-enter key" }));

    expect(await screen.findByText(/stored API key cannot be accessed/i)).toBeTruthy();
    const keyInput = screen.getByLabelText("OPENAI_API_KEY");
    const saveButton = screen.getByRole("button", { name: "Save changes" });
    expect((saveButton as HTMLButtonElement).disabled).toBe(true);

    fireEvent.change(keyInput, { target: { value: "replacement-key" } });
    expect((saveButton as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(saveButton);

    await waitFor(() => {
      const patch = fetchMock.mock.calls.find(([url, init]) => (
        String(url).endsWith("/api/providers/provider-blocked") && init?.method === "PATCH"
      ));
      expect(patch).toBeTruthy();
      expect(JSON.parse(String(patch?.[1]?.body))).toMatchObject({
        api_key: "replacement-key",
      });
    });
    unmount();
  });
});
