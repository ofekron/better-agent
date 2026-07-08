import { fireEvent, render, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { SessionSelectorControls } from "../src/components/SessionSelectorControls";
import type { Provider, Session } from "../src/types";
import { cacheProviderModels } from "../src/utils/providerCache";

vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (_key: string, options?: string | { percent?: number; defaultValue?: string }) => {
      if (typeof options === "object" && options.defaultValue) {
        return options.defaultValue.replace("{{percent}}", String(options.percent));
      }
      return typeof options === "string" ? options : _key;
    },
  }),
}));

afterEach(() => {
  vi.restoreAllMocks();
});

function provider(overrides: Partial<Provider> = {}): Provider {
  return {
    id: "claude",
    name: "Claude",
    kind: "claude",
    mode: "subscription",
    base_url: "",
    config_dir: "",
    custom_models: [],
    default_model: "sonnet",
    runner: "claude",
    runner_options: ["claude"],
    suspended: false,
    reasoning_effort_options: ["low", "medium", "high"],
    default_reasoning_effort: "medium",
    permission_options: {},
    default_permission: {},
    has_api_key: false,
    supports_fork: true,
    supports_manager_mode: true,
    supports_rewind: true,
    supports_steering: true,
    supports_native_subagents: false,
    supports_reasoning_effort: true,
    capability_overrides: {},
    ...overrides,
  };
}

function session(overrides: Partial<Session> = {}): Session {
  return {
    id: "s1",
    name: "Session",
    model: "sonnet",
    provider_id: "claude",
    cwd: "/tmp/project",
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    messages: [],
    ...overrides,
  };
}

describe("SessionSelectorControls picker interactions", () => {
  it("stages model edits until OK and discards them on Cancel", async () => {
    cacheProviderModels("claude", ["sonnet", "opus"]);
    vi.spyOn(globalThis, "fetch").mockImplementation(() => Promise.resolve(
      new Response(JSON.stringify({ models: ["sonnet", "opus"] }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    ));
    const onChange = vi.fn();

    const { getByRole, queryByRole } = render(
      <SessionSelectorControls
        session={session()}
        providers={[provider()]}
        onChange={onChange}
      />,
    );

    const modelSelect = () => document.querySelectorAll<HTMLSelectElement>(".session-model-picker-field select")[1];

    fireEvent.click(getByRole("button", { name: "Change session model" }));
    await waitFor(() => expect(modelSelect()?.querySelector('option[value="opus"]')).toBeTruthy());
    fireEvent.change(modelSelect()!, { target: { value: "opus" } });
    fireEvent.click(getByRole("button", { name: "Cancel" }));

    expect(onChange).not.toHaveBeenCalled();
    expect(queryByRole("dialog")).toBeNull();

    fireEvent.click(getByRole("button", { name: "Change session model" }));
    await waitFor(() => expect(modelSelect()?.querySelector('option[value="opus"]')).toBeTruthy());
    fireEvent.change(modelSelect()!, { target: { value: "opus" } });
    fireEvent.click(getByRole("button", { name: "OK" }));

    await waitFor(() => {
      expect(onChange).toHaveBeenCalledTimes(1);
      expect(onChange).toHaveBeenCalledWith({ model: "opus" });
    });
  });

  it("shows provider quota remaining in model options", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/quota-status")) {
        return Promise.resolve(
          new Response(
            JSON.stringify({
              providers: {
                "claude::": {
                  provider: "claude",
                  label: "Claude",
                  supported: true,
                  windows: [{ key: "weekly", label: "Weekly", used_percent: 57 }],
                },
              },
            }),
            { status: 200, headers: { "Content-Type": "application/json" } },
          ),
        );
      }
      return Promise.resolve(
        new Response(JSON.stringify({ models: ["sonnet", "opus"] }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      );
    });

    const { getByRole } = render(
      <SessionSelectorControls
        session={session()}
        providers={[provider()]}
        onChange={vi.fn()}
      />,
    );

    fireEvent.click(getByRole("button", { name: "Change session model" }));

    await waitFor(() => {
      const modelSelect = document.querySelectorAll<HTMLSelectElement>(".session-model-picker-field select")[1];
      expect(modelSelect?.querySelector('option[value="opus"]')?.textContent).toBe("opus · 43% left");
    });
  });
});
