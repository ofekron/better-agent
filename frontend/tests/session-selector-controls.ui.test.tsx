import { fireEvent, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SessionSelectorControls } from "../src/components/SessionSelectorControls";
import type { Session } from "../src/types";
import {
  makeProvider,
  makeRuntimeProfilesSnapshot,
  makeRuntimeProfile,
} from "./fixtures";

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

beforeEach(() => {
  localStorage.clear();
});

afterEach(() => {
  vi.restoreAllMocks();
});

function session(overrides: Partial<Session> = {}): Session {
  return {
    id: "s1",
    name: "Session",
    model: "sonnet",
    provider_id: "claude",
    runtime_profile_id: "rp-1",
    cwd: "/tmp/project",
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    messages: [],
    ...overrides,
  };
}

function catalog(models: string[]) {
  return {
    provider_id: "claude",
    provider_generation: "generation-1",
    authoritative: false,
    status: "current",
    models,
    models_current: true,
    retired: [],
    retired_models: [],
    last_refreshed_at: 1,
    reason: "",
    authority_fingerprint: "",
    last_known_good: null,
    runtime_profiles: [],
  };
}

const snapshot = makeRuntimeProfilesSnapshot({
  runtime_profiles: [makeRuntimeProfile({ default_model: "sonnet" })],
});

function jsonResponse(body: unknown) {
  return Promise.resolve(
    new Response(JSON.stringify(body), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }),
  );
}

describe("SessionSelectorControls picker interactions", () => {
  it("stages model edits until OK and discards them on Cancel", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/runtime-profiles")) return jsonResponse(snapshot);
      return jsonResponse(catalog(["sonnet", "opus"]));
    });
    const onChange = vi.fn();

    const { getByRole, queryByRole } = render(
      <SessionSelectorControls
        session={session()}
        providers={[makeProvider()]}
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
      expect(onChange).toHaveBeenCalledWith({
        model: "opus",
        reasoning_effort: "medium",
      });
    });
  });

  it("shows provider quota remaining in model options", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/runtime-profiles")) return jsonResponse(snapshot);
      if (url.includes("/quota-status")) {
        return jsonResponse({
          // Keyed by provider id (providerQuotaKey prefers id over
          // "<kind>::<config_dir>"), matching the extension's response.
          providers: {
            claude: {
              provider: "claude",
              label: "Claude",
              supported: true,
              windows: [{ key: "weekly", label: "Weekly", used_percent: 57 }],
            },
          },
        });
      }
      return jsonResponse(catalog(["sonnet", "opus"]));
    });

    const { getByRole } = render(
      <SessionSelectorControls
        session={session()}
        providers={[makeProvider()]}
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
