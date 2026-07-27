import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import "../src/i18n";
import { eventBus } from "../src/lib/eventBus";
import type { HarnessProfile } from "../src/types";
import {
  GROUP_DISABLED_BUILTIN_EXTENSIONS,
  GROUP_DISABLED_BUILTIN_TOOLS,
  GROUP_DISABLED_RUNTIME_SKILLS,
  GROUP_SKILLS,
  type HarnessDescriptor,
} from "../src/components/harness/types";

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

const descriptor: HarnessDescriptor = {
  extensions: [
    {
      id: "ofek-dev.fixture",
      name: "Fixture Extension",
      description: "",
      required: false,
      enabled: true,
      runtime_ready: true,
      runtime_not_ready_reason: "",
      groups: [
        {
          id: GROUP_SKILLS,
          scope: "profile",
          control: "item_toggles",
          value: null,
          items: [
            {
              name: "fixture-skill",
              label: "Fixture skill",
              description: "",
              detail: "",
              default_enabled: true,
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

function profile(revision: string, enabled: boolean): HarnessProfile {
  return {
    id: "default",
    name: "Default",
    revision,
    fields: {
      extension_instances: {
        "ofek-dev.fixture": {
          mcp_servers: { resolved: [], override: null },
          skills: {
            resolved: enabled ? ["fixture-skill"] : [],
            override: null,
          },
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
});

describe("harness settings save latency", () => {
  it("uses the write response and suppresses the local refetch echo", async () => {
    trackedFetch.mockResolvedValue(response({
      profiles: [{ id: "default", name: "Default", revision: "" }],
    }));
    fetchDescriptor.mockResolvedValue(descriptor);
    fetchProfile.mockResolvedValue(profile("r1", true));
    writeFields.mockImplementation(async () => {
      eventBus.publish("harness_profiles_changed", {
        action: "fields_updated",
        profile_id: "default",
        revision: "r2",
      });
      return profile("r2", false);
    });

    render(<HarnessSettingsEditor />);
    await screen.findByText("Fixture Extension");
    const initialListCalls = trackedFetch.mock.calls.length;
    const initialProfileCalls = fetchProfile.mock.calls.length;
    const initialDescriptorCalls = fetchDescriptor.mock.calls.length;

    fireEvent.click(screen.getByLabelText("Fixture skill"));

    await waitFor(() => expect(writeFields).toHaveBeenCalledTimes(1));
    await waitFor(() => expect((screen.getByLabelText("Fixture skill") as HTMLInputElement).checked).toBe(false));

    expect(trackedFetch).toHaveBeenCalledTimes(initialListCalls);
    expect(fetchProfile).toHaveBeenCalledTimes(initialProfileCalls);
    expect(fetchDescriptor).toHaveBeenCalledTimes(initialDescriptorCalls);
  });

  it("reconciles after a concurrent in-flight profile event", async () => {
    trackedFetch.mockResolvedValue(response({
      profiles: [{ id: "default", name: "Default", revision: "" }],
    }));
    fetchDescriptor.mockResolvedValue(descriptor);
    fetchProfile.mockResolvedValue(profile("r1", true));
    writeFields.mockImplementation(async () => {
      eventBus.publish("harness_profiles_changed", {
        action: "fields_updated",
        profile_id: "other-profile",
        revision: "other-r2",
      });
      return profile("r2", false);
    });

    render(<HarnessSettingsEditor />);
    await screen.findByText("Fixture Extension");
    const initialListCalls = trackedFetch.mock.calls.length;
    const initialProfileCalls = fetchProfile.mock.calls.length;
    const initialDescriptorCalls = fetchDescriptor.mock.calls.length;

    fireEvent.click(screen.getByLabelText("Fixture skill"));

    await waitFor(() => expect(writeFields).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(fetchProfile).toHaveBeenCalledTimes(initialProfileCalls + 1));
    expect(trackedFetch).toHaveBeenCalledTimes(initialListCalls + 1);
    expect(fetchDescriptor).toHaveBeenCalledTimes(initialDescriptorCalls + 1);
  });

  it("reconciles after a same-profile in-flight event with a different revision", async () => {
    trackedFetch.mockResolvedValue(response({
      profiles: [{ id: "default", name: "Default", revision: "" }],
    }));
    fetchDescriptor.mockResolvedValue(descriptor);
    fetchProfile.mockResolvedValue(profile("r1", true));
    writeFields.mockImplementation(async () => {
      eventBus.publish("harness_profiles_changed", {
        action: "fields_updated",
        profile_id: "default",
        revision: "external-r3",
      });
      return profile("r2", false);
    });

    render(<HarnessSettingsEditor />);
    await screen.findByText("Fixture Extension");
    const initialListCalls = trackedFetch.mock.calls.length;
    const initialProfileCalls = fetchProfile.mock.calls.length;
    const initialDescriptorCalls = fetchDescriptor.mock.calls.length;

    fireEvent.click(screen.getByLabelText("Fixture skill"));

    await waitFor(() => expect(writeFields).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(fetchProfile).toHaveBeenCalledTimes(initialProfileCalls + 1));
    expect(trackedFetch).toHaveBeenCalledTimes(initialListCalls + 1);
    expect(fetchDescriptor).toHaveBeenCalledTimes(initialDescriptorCalls + 1);
  });

  it("reconciles after an in-flight extension config event", async () => {
    trackedFetch.mockResolvedValue(response({
      profiles: [{ id: "default", name: "Default", revision: "" }],
    }));
    fetchDescriptor.mockResolvedValue(descriptor);
    fetchProfile.mockResolvedValue(profile("r1", true));
    writeFields.mockImplementation(async () => {
      eventBus.publish("extension.config.skills", {});
      eventBus.publish("harness_profiles_changed", {
        action: "fields_updated",
        profile_id: "default",
        revision: "r2",
      });
      return profile("r2", false);
    });

    render(<HarnessSettingsEditor />);
    await screen.findByText("Fixture Extension");
    const initialListCalls = trackedFetch.mock.calls.length;
    const initialProfileCalls = fetchProfile.mock.calls.length;
    const initialDescriptorCalls = fetchDescriptor.mock.calls.length;

    fireEvent.click(screen.getByLabelText("Fixture skill"));

    await waitFor(() => expect(writeFields).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(fetchProfile).toHaveBeenCalledTimes(initialProfileCalls + 1));
    expect(trackedFetch).toHaveBeenCalledTimes(initialListCalls + 1);
    expect(fetchDescriptor).toHaveBeenCalledTimes(initialDescriptorCalls + 1);
  });

  it("ignores unrelated in-flight extension UI events", async () => {
    trackedFetch.mockResolvedValue(response({
      profiles: [{ id: "default", name: "Default", revision: "" }],
    }));
    fetchDescriptor.mockResolvedValue(descriptor);
    fetchProfile.mockResolvedValue(profile("r1", true));
    writeFields.mockImplementation(async () => {
      eventBus.publish("extension.ui.frontend_modules", {});
      eventBus.publish("harness_profiles_changed", {
        action: "fields_updated",
        profile_id: "default",
        revision: "r2",
      });
      return profile("r2", false);
    });

    render(<HarnessSettingsEditor />);
    await screen.findByText("Fixture Extension");
    const initialListCalls = trackedFetch.mock.calls.length;
    const initialProfileCalls = fetchProfile.mock.calls.length;
    const initialDescriptorCalls = fetchDescriptor.mock.calls.length;

    fireEvent.click(screen.getByLabelText("Fixture skill"));

    await waitFor(() => expect(writeFields).toHaveBeenCalledTimes(1));
    await waitFor(() => expect((screen.getByLabelText("Fixture skill") as HTMLInputElement).checked).toBe(false));

    expect(trackedFetch).toHaveBeenCalledTimes(initialListCalls);
    expect(fetchProfile).toHaveBeenCalledTimes(initialProfileCalls);
    expect(fetchDescriptor).toHaveBeenCalledTimes(initialDescriptorCalls);
  });
});
