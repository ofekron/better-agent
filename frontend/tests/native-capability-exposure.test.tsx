import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import "../src/i18n";
import { eventBus } from "../src/lib/eventBus";
import type { HarnessProfile } from "../src/types";
import {
  GROUP_DISABLED_BUILTIN_EXTENSIONS,
  GROUP_DISABLED_BUILTIN_TOOLS,
  GROUP_DISABLED_RUNTIME_SKILLS,
  type HarnessDescriptor,
} from "../src/components/harness/types";

// Native capability exposure ("expose this skill/instruction to the native
// provider CLI's own config") is a harness-profile field
// (backend/harness_fields.py GROUP_NATIVE_EXPOSURE = "native_exposure",
// scope=global) rendered generically by HarnessGroup's item-toggle row and
// written through the same field-write endpoint as every other harness
// control. It is no longer part of ExtensionUiSettingsSection (see "Move
// extension settings into the harness profile editor", 63ddc5192): that
// page is install/enable/uninstall only now.
const trackedFetch = vi.fn();
const fetchDescriptor = vi.fn();
const fetchProfile = vi.fn();
const writeFields = vi.fn();

vi.mock("../src/api", () => ({ API: "" }));
vi.mock("../src/progress/store", () => ({
  trackedFetch: (...args: unknown[]) => trackedFetch(...args),
}));
vi.mock("../src/components/harness/api", async () => {
  const actual = await vi.importActual<typeof import("../src/components/harness/api")>(
    "../src/components/harness/api",
  );
  return {
    ...actual,
    fetchDescriptor: (...args: unknown[]) => fetchDescriptor(...args),
    fetchProfile: (...args: unknown[]) => fetchProfile(...args),
    writeFields: (...args: unknown[]) => writeFields(...args),
  };
});

const { HarnessSettingsEditor } = await import("../src/components/HarnessSettingsEditor");

function response(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    json: async () => body,
  } as Response;
}

const EXTENSION_ID = "ofek.native-harness";

function descriptor(exposed: boolean): HarnessDescriptor {
  return {
    extensions: [
      {
        id: EXTENSION_ID,
        name: "Native Harness",
        description: "",
        required: false,
        enabled: true,
        runtime_ready: true,
        runtime_not_ready_reason: "",
        groups: [
          {
            id: "native_exposure",
            scope: "global",
            control: "item_toggles",
            value: null,
            items: [
              {
                name: "skill:reviewer",
                label: "reviewer",
                description: "",
                detail: "skill",
                default_enabled: exposed,
                locked_by: [],
              },
            ],
          },
        ],
      },
    ],
    builtin_extensions: {
      id: GROUP_DISABLED_BUILTIN_EXTENSIONS,
      scope: "profile",
      control: "item_toggles",
      value: null,
      items: [],
    },
    builtin_tools: {
      id: GROUP_DISABLED_BUILTIN_TOOLS,
      scope: "profile",
      control: "item_toggles",
      value: null,
      items: [],
    },
    runtime_skills: {
      id: GROUP_DISABLED_RUNTIME_SKILLS,
      scope: "profile",
      control: "item_toggles",
      value: null,
      items: [],
    },
  };
}

function profile(revision: string): HarnessProfile {
  return {
    id: "default",
    name: "Default",
    revision,
    fields: {
      extension_instances: {
        [EXTENSION_ID]: {
          mcp_servers: { resolved: [], override: null },
          skills: { resolved: [], override: null },
          instruction_names: { resolved: [], override: null },
          setting_overlays: {},
        },
      },
      disabled_builtin_extensions: { resolved: [], override: null },
      disabled_builtin_tools: { resolved: [], override: null },
      disabled_runtime_skills: { resolved: [], override: null },
      instruction_sources: {},
    },
  } as HarnessProfile;
}

afterEach(() => {
  trackedFetch.mockReset();
  fetchDescriptor.mockReset();
  fetchProfile.mockReset();
  writeFields.mockReset();
  vi.restoreAllMocks();
});

describe("native capability exposure", () => {
  it("persists an eligible capability and reflects the backend-confirmed state", async () => {
    trackedFetch.mockResolvedValue(response({
      profiles: [{ id: "default", name: "Default", revision: "" }],
    }));
    fetchDescriptor.mockResolvedValueOnce(descriptor(false)).mockResolvedValueOnce(descriptor(true));
    fetchProfile.mockResolvedValue(profile("r1"));
    // The real backend broadcasts extension.config + extension.harness.default
    // when a global-scoped field write changes extension-owned state
    // (harness_profiles_api.py: `if changes_global_state: ... broadcast(...)`),
    // which HarnessSettingsEditor picks up to refetch the descriptor.
    writeFields.mockImplementation(async () => {
      eventBus.publish("extension.config", {});
      return profile("r1");
    });

    render(<HarnessSettingsEditor />);

    const toggle = await screen.findByRole("checkbox", { name: "reviewer" });
    expect((toggle as HTMLInputElement).checked).toBe(false);

    fireEvent.click(toggle);

    await waitFor(() => expect(writeFields).toHaveBeenCalledTimes(1));
    expect(writeFields).toHaveBeenCalledWith(
      "default",
      [{ path: ["extension_instances", EXTENSION_ID, "native_exposure", "skill:reviewer"], value: true }],
      "r1",
    );
    await waitFor(() =>
      expect((screen.getByRole("checkbox", { name: "reviewer" }) as HTMLInputElement).checked).toBe(true),
    );
  });

  it("keeps backend state and shows the server error when exposure is rejected", async () => {
    trackedFetch.mockResolvedValue(response({
      profiles: [{ id: "default", name: "Default", revision: "" }],
    }));
    fetchDescriptor.mockResolvedValue(descriptor(false));
    fetchProfile.mockResolvedValue(profile("r1"));
    writeFields.mockRejectedValue(new Error("Native installation is blocked"));

    render(<HarnessSettingsEditor />);

    const toggle = await screen.findByRole("checkbox", { name: "reviewer" });
    fireEvent.click(toggle);

    expect((await screen.findByRole("alert")).textContent).toBe(
      "Failed to update harness profile: Native installation is blocked",
    );
    expect((toggle as HTMLInputElement).checked).toBe(false);
  });
});
