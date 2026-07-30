import { act, fireEvent, render, waitFor } from "@testing-library/react";
import type { ReactElement } from "react";
import {
  afterAll,
  beforeAll,
  beforeEach,
  describe,
  expect,
  it,
  vi,
} from "vitest";

import {
  NEW_SESSION_PROMPT_TESTID,
  NewSessionModal,
  type NewSessionExtensionOption,
  type SessionConfig,
} from "../src/components/NewSessionModal";
import type { Provider } from "../src/types";
import { completeOp, startOp } from "../src/progress/store";
import { cacheProviders } from "../src/utils/providerCache";

vi.mock("../src/hooks/useMachines", () => ({
  useMachines: () => ({ machines: [] }),
}));

vi.mock("../src/hooks/useLocalNodeId", () => ({
  useLocalNodeId: () => "primary",
}));

vi.mock("../src/utils/fetchRetry", () => ({
  fetchWithRetry: (input: RequestInfo | URL, init?: RequestInit) =>
    fetch(input, init),
}));

const actWarnings: string[] = [];
let consoleErrorSpy: ReturnType<typeof vi.spyOn>;

beforeAll(() => {
  const originalConsoleError = console.error.bind(console);
  consoleErrorSpy = vi.spyOn(console, "error").mockImplementation((...args) => {
    originalConsoleError(...args);
    const message = args.map(String).join(" ");
    if (message.includes("not wrapped in act")) {
      actWarnings.push(message);
    }
  });
});

afterAll(() => {
  try {
    expect(actWarnings).toEqual([]);
  } finally {
    consoleErrorSpy.mockRestore();
  }
});

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

async function renderSettled(ui: ReactElement) {
  const view = render(ui);
  await act(async () => {});
  return view;
}

async function clickSettled(element: Element) {
  await act(async () => {
    fireEvent.click(element);
  });
}

describe("NewSessionModal offline provider cache", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it("only shows creating for this modal's own submission", async () => {
    cacheProviders([provider], provider.id);
    vi.spyOn(globalThis, "fetch").mockRejectedValue(new TypeError("offline"));
    let resolveCreate!: () => void;
    const createPending = new Promise<void>((resolve) => {
      resolveCreate = resolve;
    });
    const onCreate = vi.fn(() => createPending);
    const modal = await renderSettled(
      <NewSessionModal
        open
        onClose={() => {}}
        onCreate={onCreate}
        defaultCwd="/tmp/project"
        projects={[]}
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

      const alert = vi.fn();
      vi.stubGlobal("alert", alert);
      onCreate.mockRejectedValueOnce(new Error("create failed"));
      fireEvent.click(createButton);
      await waitFor(() => expect(createButton.disabled).toBe(false));
      expect(alert).toHaveBeenCalledWith("create failed");
    } finally {
      act(() => completeOp("session:create"));
      vi.unstubAllGlobals();
    }
  });

  it("keeps the prompt draft when create fails and clears it only on success", async () => {
    cacheProviders([provider], provider.id);
    vi.spyOn(globalThis, "fetch").mockRejectedValue(new TypeError("offline"));
    const onCreate = vi
      .fn<() => Promise<boolean>>()
      .mockResolvedValueOnce(false)
      .mockResolvedValueOnce(true);

    const modal = await renderSettled(
      <NewSessionModal
        open
        onClose={() => {}}
        onCreate={onCreate}
        defaultCwd="/tmp/project"
        projects={[]}
      />,
    );

    await waitFor(() => {
      expect(modal.container.querySelector(".ns-create-primary")).toBeTruthy();
    });
    const textarea = modal.getByTestId(NEW_SESSION_PROMPT_TESTID) as HTMLTextAreaElement;
    fireEvent.change(textarea, { target: { value: "my important prompt" } });
    expect(localStorage.getItem("better-agent-new-session-prompt-draft")).toBe(
      "my important prompt",
    );

    const createButton = modal.container.querySelector(".ns-create-primary") as HTMLButtonElement;

    // Failed create (onCreate → false): the draft and textarea must survive.
    await clickSettled(createButton);
    expect(onCreate).toHaveBeenCalledTimes(1);
    expect(localStorage.getItem("better-agent-new-session-prompt-draft")).toBe(
      "my important prompt",
    );
    expect(textarea.value).toBe("my important prompt");

    // Successful create: the draft is cleared.
    await clickSettled(createButton);
    expect(onCreate).toHaveBeenCalledTimes(2);
    await waitFor(() => {
      expect(localStorage.getItem("better-agent-new-session-prompt-draft")).toBeNull();
    });
  });

  it("offers all create actions and remembers the last selection", async () => {
    cacheProviders([provider], provider.id);
    vi.spyOn(globalThis, "fetch").mockRejectedValue(new TypeError("offline"));
    const onCreate = vi.fn();

    const modal = await renderSettled(
      <NewSessionModal
        open
        onClose={() => {}}
        onCreate={onCreate}
        defaultCwd="/tmp/project"
        projects={[]}
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
    await clickSettled(
      modal.getByRole("button", { name: "newSession.createAndSend" }),
    );

    expect(onCreate.mock.calls.map((call) => call[2])).toEqual(["send", "send"]);
    expect(JSON.parse(localStorage.getItem("better-agent-new-session-defaults") ?? "{}"))
      .toEqual(expect.objectContaining({ creationAction: "send" }));

    modal.unmount();
    const reopened = await renderSettled(
      <NewSessionModal
        open
        onClose={() => {}}
        onCreate={onCreate}
        defaultCwd="/tmp/project"
        projects={[]}
      />,
    );

    await waitFor(() => {
      expect(reopened.getByRole("button", { name: "newSession.createAndSend" })).toBeTruthy();
    });
    await clickSettled(
      reopened.getByRole("button", { name: "newSession.createAndSend" }),
    );
    expect(onCreate.mock.calls.at(-1)?.[2]).toBe("send");
  });

  it("shows the harness profile capability entry point", async () => {
    const { getByText, getByRole } = await renderSettled(
      <NewSessionModal
        open
        onClose={() => {}}
        onCreate={vi.fn()}
        defaultCwd="/tmp/project"
        projects={[]}
      />,
    );

    expect(getByText("Harness profile")).toBeTruthy();
    expect(getByRole("combobox", { name: "Harness profile" })).toBeTruthy();
  });

  it("creates with cached provider and model when provider fetches fail", async () => {
    cacheProviders([provider], provider.id);
    localStorage.setItem(
      "better-agent-new-session-defaults",
      JSON.stringify({
        orchestrationMode: "native",
        main: { providerId: provider.id, model: "cached-opus", reasoningEffort: "high", runner: "native", permission: {} },
      }),
    );
    vi.spyOn(globalThis, "fetch").mockRejectedValue(new TypeError("offline"));
    const onCreate = vi.fn<(config: SessionConfig) => void>();

    const { container } = await renderSettled(
      <NewSessionModal
        open
        onClose={() => {}}
        onCreate={onCreate}
        defaultCwd="/tmp/project"
        projects={[]}
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

    await clickSettled(
      container.querySelector(".modal-footer .btn-primary")!,
    );

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
    localStorage.setItem(
      "better-agent-new-session-defaults",
      JSON.stringify({
        orchestrationMode: "team",
        main: { providerId: nativeOnlyProvider.id, model: "cached-default" },
      }),
    );
    vi.spyOn(globalThis, "fetch").mockRejectedValue(new TypeError("offline"));
    const onCreate = vi.fn<(config: SessionConfig) => void>();

    const { container, queryByText } = await renderSettled(
      <NewSessionModal
        open
        onClose={() => {}}
        onCreate={onCreate}
        defaultCwd="/tmp/project"
        projects={[]}
      />,
    );

    await waitFor(() => {
      expect(container.querySelector(`option[value="${nativeOnlyProvider.id}"]`)).toBeTruthy();
    });

    expect(queryByText("newSession.orchestration")).toBeNull();
    expect(queryByText("orchestration.nativeDirect")).toBeNull();
    expect(queryByText("orchestration.managerWorkers")).toBeNull();

    await clickSettled(
      container.querySelector(".modal-footer .btn-primary")!,
    );

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
    vi.spyOn(globalThis, "fetch").mockRejectedValue(new TypeError("offline"));
    const onCreate = vi.fn<(config: SessionConfig) => void>();

    const { container, getByLabelText } = await renderSettled(
      <NewSessionModal
        open
        onClose={() => {}}
        onCreate={onCreate}
        defaultCwd="/tmp/project"
        projects={[]}
      />,
    );

    await waitFor(() => {
      expect(container.querySelector(`option[value="${provider.id}"]`)).toBeTruthy();
    });

    fireEvent.click(getByLabelText("newSession.fileEdit"));

    expect(container.querySelector(".ns-file-picker-input")).toBeNull();
    expect(container.querySelector(".ns-file-picker-browse")).toBeNull();

    await clickSettled(
      container.querySelector(".modal-footer .btn-primary")!,
    );

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
    vi.spyOn(globalThis, "fetch").mockRejectedValue(new TypeError("offline"));
    const onCreate = vi.fn<(config: SessionConfig) => void>();
    const options: NewSessionExtensionOption[] = [
      {
        id: "demo_option",
        extensionId: "ofek-dev.demo",
        label: "Demo option",
        defaultValue: false,
        applyToSessionConfig: (value) => ({
          harnessProfileId: value ? "demo-profile" : "",
        }),
      },
    ];

    const { container, getByLabelText } = await renderSettled(
      <NewSessionModal
        open
        onClose={() => {}}
        onCreate={onCreate}
        defaultCwd="/tmp/project"
        projects={[]}
        extensionOptions={options}
      />,
    );

    await waitFor(() => {
      expect(container.querySelector(`option[value="${provider.id}"]`)).toBeTruthy();
    });

    fireEvent.click(getByLabelText("Demo option"));
    await clickSettled(
      container.querySelector(".modal-footer .btn-primary")!,
    );

    expect(onCreate).toHaveBeenCalledWith(
      expect.objectContaining({
        harnessProfileId: "demo-profile",
      }),
      undefined,
      "send-and-open",
    );
  });

  it("keeps same-id extension options isolated by extension id", async () => {
    cacheProviders([provider], provider.id);
    vi.spyOn(globalThis, "fetch").mockRejectedValue(new TypeError("offline"));
    const onCreate = vi.fn<(config: SessionConfig) => void>();

    const { container, getByLabelText } = await renderSettled(
      <NewSessionModal
        open
        onClose={() => {}}
        onCreate={onCreate}
        defaultCwd="/tmp/project"
        projects={[]}
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
    await clickSettled(
      container.querySelector(".modal-footer .btn-primary")!,
    );

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

describe("NewSessionModal providers load failure", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it("surfaces a retry affordance when the providers fetch fails, and recovers on retry", async () => {
    let providersShouldFail = true;
    vi.spyOn(globalThis, "fetch").mockImplementation((input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url.includes("/api/providers") && !providersShouldFail) {
        return Promise.resolve({
          ok: true,
          json: async () => ({ providers: [provider], default_provider_id: provider.id }),
        } as Response);
      }
      return Promise.reject(new TypeError("offline"));
    });

    const modal = await renderSettled(
      <NewSessionModal
        open
        onClose={() => {}}
        onCreate={vi.fn()}
        defaultCwd="/tmp/project"
        projects={[]}
      />,
    );

    await waitFor(() => {
      expect(modal.getByTestId("new-session-providers-error")).toBeTruthy();
    });
    expect(modal.container.querySelector(`option[value="${provider.id}"]`)).toBeNull();

    providersShouldFail = false;
    await clickSettled(
      modal.getByRole("button", { name: "newSession.providersRetry" }),
    );

    await waitFor(() => {
      expect(modal.queryByTestId("new-session-providers-error")).toBeNull();
    });
    expect(modal.container.querySelector(`option[value="${provider.id}"]`)).toBeTruthy();
  });
});
