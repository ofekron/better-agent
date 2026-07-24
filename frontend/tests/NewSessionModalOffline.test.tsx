import { act, fireEvent, render, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  NewSessionModal,
  type NewSessionExtensionOption,
  type SessionConfig,
} from "../src/components/NewSessionModal";
import type { Provider } from "../src/types";
import { completeOp, startOp } from "../src/progress/store";
import { cacheProviderModels, cacheProviders } from "../src/utils/providerCache";

vi.mock("../src/hooks/useMachines", () => ({
  useMachines: () => ({ machines: [] }),
}));

vi.mock("../src/hooks/useLocalNodeId", () => ({
  useLocalNodeId: () => "primary",
}));

const provider: Provider = {
  id: "cached-claude",
  name: "Cached Claude",
  kind: "claude",
  mode: "subscription",
  base_url: "",
  config_dir: "",
  custom_models: [],
  default_model: "cached-default",
  runner: "native",
  runner_options: ["native"],
  suspended: false,
  reasoning_effort_options: ["low", "medium", "high", "xhigh"],
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
};

const nativeOnlyProvider: Provider = {
  ...provider,
  id: "cached-native-only",
  name: "Cached Native Only",
  supports_manager_mode: false,
};

const capabilityPickerClient = {
  listCapabilityPickerSources: vi.fn(async () => ({ sources: [] })),
};

describe("NewSessionModal offline provider cache", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it("only shows creating for this modal's own submission", async () => {
    cacheProviders([provider], provider.id);
    cacheProviderModels(provider.id, ["cached-default"]);
    vi.spyOn(globalThis, "fetch").mockRejectedValue(new TypeError("offline"));
    let resolveCreate!: () => void;
    const createPending = new Promise<void>((resolve) => {
      resolveCreate = resolve;
    });
    const onCreate = vi.fn(() => createPending);
    const modal = render(
      <NewSessionModal
        open
        onClose={() => {}}
        onCreate={onCreate}
        defaultCwd="/tmp/project"
        projects={[]}
        capabilityPickerClient={capabilityPickerClient}
      />,
    );

    await waitFor(() => {
      expect(modal.container.querySelector(".ns-create-primary")).toBeTruthy();
    });
    const createButton = modal.container.querySelector(".ns-create-primary") as HTMLButtonElement;

    act(() => startOp("session:create"));
    try {
      expect(createButton.disabled).toBe(false);
      expect(createButton.dataset.progressInflight).toBeUndefined();

      fireEvent.click(createButton);
      fireEvent.click(createButton);
      expect(onCreate).toHaveBeenCalledTimes(1);
      expect(createButton.disabled).toBe(true);
      expect(createButton.dataset.progressInflight).toBe("1");

      act(() => resolveCreate());
      await waitFor(() => expect(createButton.disabled).toBe(false));
      expect(createButton.dataset.progressInflight).toBeUndefined();

      const alert = vi.spyOn(window, "alert").mockImplementation(() => {});
      onCreate.mockRejectedValueOnce(new Error("create failed"));
      fireEvent.click(createButton);
      await waitFor(() => expect(createButton.disabled).toBe(false));
      expect(alert).toHaveBeenCalledWith("create failed");
    } finally {
      act(() => completeOp("session:create"));
    }
  });

  it("offers all create actions and remembers the last selection", async () => {
    cacheProviders([provider], provider.id);
    cacheProviderModels(provider.id, ["cached-default"]);
    vi.spyOn(globalThis, "fetch").mockRejectedValue(new TypeError("offline"));
    const onCreate = vi.fn();

    const modal = render(
      <NewSessionModal
        open
        onClose={() => {}}
        onCreate={onCreate}
        defaultCwd="/tmp/project"
        projects={[]}
        capabilityPickerClient={capabilityPickerClient}
      />,
    );

    await waitFor(() => {
      expect(modal.getByRole("button", { name: "newSession.createAndSendAndOpen" })).toBeTruthy();
    });

    fireEvent.click(modal.getByRole("button", {
      name: "newSession.createAndSendAndOpen — newSession.create",
    }));
    expect(modal.getAllByRole("menuitem")).toHaveLength(3);
    fireEvent.click(modal.getByRole("menuitem", { name: "newSession.createAndSend" }));
    await waitFor(() => {
      expect(modal.getByRole("button", { name: "newSession.createAndSend" })).toBeTruthy();
    });
    fireEvent.click(modal.getByRole("button", { name: "newSession.createAndSend" }));

    expect(onCreate.mock.calls.map((call) => call[2])).toEqual(["send", "send"]);
    expect(JSON.parse(localStorage.getItem("better-agent-new-session-defaults") ?? "{}"))
      .toEqual(expect.objectContaining({ creationAction: "send" }));

    modal.unmount();
    const reopened = render(
      <NewSessionModal
        open
        onClose={() => {}}
        onCreate={onCreate}
        defaultCwd="/tmp/project"
        projects={[]}
        capabilityPickerClient={capabilityPickerClient}
      />,
    );

    await waitFor(() => {
      expect(reopened.getByRole("button", { name: "newSession.createAndSend" })).toBeTruthy();
    });
    fireEvent.click(reopened.getByRole("button", { name: "newSession.createAndSend" }));
    expect(onCreate.mock.calls.at(-1)?.[2]).toBe("send");
  });

  it("shows the session capability picker entry point", () => {
    const { getByText, getByRole } = render(
      <NewSessionModal
        open
        onClose={() => {}}
        onCreate={vi.fn()}
        defaultCwd="/tmp/project"
        projects={[]}
        capabilityPickerClient={capabilityPickerClient}
      />,
    );

    expect(getByText("Capabilities")).toBeTruthy();
    expect(getByRole("button", { name: /Add capability/ })).toBeTruthy();
  });

  it("creates with cached provider and model when provider fetches fail", async () => {
    cacheProviders([provider], provider.id);
    cacheProviderModels(provider.id, ["cached-default", "cached-opus"]);
    localStorage.setItem(
      "better-agent-new-session-defaults",
      JSON.stringify({
        orchestrationMode: "native",
        main: { providerId: provider.id, model: "cached-opus", reasoningEffort: "high", runner: "native", permission: {} },
      }),
    );
    vi.spyOn(globalThis, "fetch").mockRejectedValue(new TypeError("offline"));
    const onCreate = vi.fn<(config: SessionConfig) => void>();

    const { container } = render(
      <NewSessionModal
        open
        onClose={() => {}}
        onCreate={onCreate}
        defaultCwd="/tmp/project"
        projects={[]}
        capabilityPickerClient={capabilityPickerClient}
      />,
    );

    await waitFor(() => {
      const providerSelect = container.querySelector(
        `option[value="${provider.id}"]`,
      )?.parentElement as HTMLSelectElement | null;
      expect(providerSelect?.value).toBe(provider.id);
    });
    const modelSelect = container.querySelector(
      'option[value="cached-opus"]',
    )?.parentElement as HTMLSelectElement | null;
    expect(modelSelect?.value).toBe("cached-opus");

    fireEvent.click(container.querySelector(".modal-footer .btn-primary")!);

    expect(onCreate).toHaveBeenCalledWith(
      expect.objectContaining({
        main: { providerId: provider.id, model: "cached-opus", reasoningEffort: "high", runner: "native", permission: {} },
      }),
      undefined,
      "send-and-open",
    );
  });

  it("hides orchestration choice and creates native when native is the only available mode", async () => {
    cacheProviders([nativeOnlyProvider], nativeOnlyProvider.id);
    cacheProviderModels(nativeOnlyProvider.id, ["cached-default"]);
    localStorage.setItem(
      "better-agent-new-session-defaults",
      JSON.stringify({
        orchestrationMode: "team",
        main: { providerId: nativeOnlyProvider.id, model: "cached-default" },
      }),
    );
    vi.spyOn(globalThis, "fetch").mockRejectedValue(new TypeError("offline"));
    const onCreate = vi.fn<(config: SessionConfig) => void>();

    const { container, queryByText } = render(
      <NewSessionModal
        open
        onClose={() => {}}
        onCreate={onCreate}
        defaultCwd="/tmp/project"
        projects={[]}
        capabilityPickerClient={capabilityPickerClient}
      />,
    );

    await waitFor(() => {
      expect(container.querySelector(`option[value="${nativeOnlyProvider.id}"]`)).toBeTruthy();
    });

    expect(queryByText("newSession.orchestration")).toBeNull();
    expect(queryByText("orchestration.nativeDirect")).toBeNull();
    expect(queryByText("orchestration.managerWorkers")).toBeNull();

    fireEvent.click(container.querySelector(".modal-footer .btn-primary")!);

    expect(onCreate).toHaveBeenCalledWith(
      expect.objectContaining({
        orchestrationMode: "native",
      }),
      undefined,
      "send-and-open",
    );
  });

  it("creates file edit sessions without selecting a file in the modal", async () => {
    cacheProviders([provider], provider.id);
    cacheProviderModels(provider.id, ["cached-default"]);
    vi.spyOn(globalThis, "fetch").mockRejectedValue(new TypeError("offline"));
    const onCreate = vi.fn<(config: SessionConfig) => void>();

    const { container, getByLabelText } = render(
      <NewSessionModal
        open
        onClose={() => {}}
        onCreate={onCreate}
        defaultCwd="/tmp/project"
        projects={[]}
        capabilityPickerClient={capabilityPickerClient}
      />,
    );

    await waitFor(() => {
      expect(container.querySelector(`option[value="${provider.id}"]`)).toBeTruthy();
    });

    fireEvent.click(getByLabelText("newSession.fileEdit"));

    expect(container.querySelector(".ns-file-picker-input")).toBeNull();
    expect(container.querySelector(".ns-file-picker-browse")).toBeNull();

    fireEvent.click(container.querySelector(".modal-footer .btn-primary")!);

    expect(onCreate).toHaveBeenCalledWith(
      expect.objectContaining({
        fileEditEnabled: true,
        fileEditPath: undefined,
      }),
      undefined,
      "send-and-open",
    );
  });

  it("lets extension options patch the created session config", async () => {
    cacheProviders([provider], provider.id);
    cacheProviderModels(provider.id, ["cached-default"]);
    vi.spyOn(globalThis, "fetch").mockRejectedValue(new TypeError("offline"));
    const onCreate = vi.fn<(config: SessionConfig) => void>();
    const options: NewSessionExtensionOption[] = [
      {
        id: "demo_option",
        extensionId: "ofek-dev.demo",
        label: "Demo option",
        defaultValue: false,
        applyToSessionConfig: (value) => ({
          capabilityContexts: value
            ? [
                {
                  source_id: "extension:ofek-dev.demo",
                  capability_id: "demo",
                  name: "Demo",
                  category: "extension",
                  outputs: [],
                },
              ]
            : [],
        }),
      },
    ];

    const { container, getByLabelText } = render(
      <NewSessionModal
        open
        onClose={() => {}}
        onCreate={onCreate}
        defaultCwd="/tmp/project"
        projects={[]}
        capabilityPickerClient={capabilityPickerClient}
        extensionOptions={options}
      />,
    );

    await waitFor(() => {
      expect(container.querySelector(`option[value="${provider.id}"]`)).toBeTruthy();
    });

    fireEvent.click(getByLabelText("Demo option"));
    fireEvent.click(container.querySelector(".modal-footer .btn-primary")!);

    expect(onCreate).toHaveBeenCalledWith(
      expect.objectContaining({
        capabilityContexts: [
          expect.objectContaining({
            source_id: "extension:ofek-dev.demo",
          }),
        ],
      }),
      undefined,
      "send-and-open",
    );
  });

  it("keeps same-id extension options isolated by extension id", async () => {
    cacheProviders([provider], provider.id);
    cacheProviderModels(provider.id, ["cached-default"]);
    vi.spyOn(globalThis, "fetch").mockRejectedValue(new TypeError("offline"));
    const onCreate = vi.fn<(config: SessionConfig) => void>();

    const { container, getByLabelText } = render(
      <NewSessionModal
        open
        onClose={() => {}}
        onCreate={onCreate}
        defaultCwd="/tmp/project"
        projects={[]}
        capabilityPickerClient={capabilityPickerClient}
        extensionOptions={[
          {
            id: "enabled",
            extensionId: "ofek-dev.first",
            label: "First extension",
            defaultValue: false,
            applyToSessionConfig: (value) => ({ preset: value ? "first" : "" }),
          },
          {
            id: "enabled",
            extensionId: "ofek-dev.second",
            label: "Second extension",
            defaultValue: false,
            applyToSessionConfig: (value) => ({ fileEditEnabled: value }),
          },
        ]}
      />,
    );

    await waitFor(() => {
      expect(container.querySelector(`option[value="${provider.id}"]`)).toBeTruthy();
    });

    fireEvent.click(getByLabelText("Second extension"));
    fireEvent.click(container.querySelector(".modal-footer .btn-primary")!);

    expect(onCreate).toHaveBeenCalledWith(
      expect.objectContaining({
        preset: "",
        fileEditEnabled: true,
      }),
      undefined,
      "send-and-open",
    );
  });
});
