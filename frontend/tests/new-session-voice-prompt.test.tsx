import { act, render, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  NEW_SESSION_PROMPT_TESTID,
  NewSessionModal,
} from "../src/components/NewSessionModal";
import { VoiceActivation } from "../src/components/VoiceActivation";
import { dictationDelta } from "../src/lib/voiceActivation";
import type {
  VoiceRecognizer,
  VoiceRecognizerHandlers,
} from "../src/lib/voiceRecognition";
import type { Provider } from "../src/types";
import { cacheProviders } from "../src/utils/providerCache";

let handlers: VoiceRecognizerHandlers | null = null;

const recognizer = {
  available: true,
  setLanguage: vi.fn(),
  setHandlers: vi.fn((next: VoiceRecognizerHandlers) => {
    handlers = next;
  }),
  start: vi.fn(),
  stop: vi.fn(),
  dispose: vi.fn(),
} satisfies VoiceRecognizer;

vi.mock("../src/lib/voiceRecognition", () => ({
  createVoiceRecognizer: () => recognizer,
}));

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

function speak(transcript: string) {
  act(() => {
    handlers?.onResult({ transcript, alternatives: [transcript] });
  });
}

describe("new session modal voice mode", () => {
  beforeEach(() => {
    localStorage.clear();
    handlers = null;
    vi.clearAllMocks();
    Object.defineProperty(document, "visibilityState", {
      value: "visible",
      configurable: true,
    });
    Object.defineProperty(window, "speechSynthesis", {
      value: { cancel: vi.fn(), speak: vi.fn() },
      configurable: true,
    });
    class TestSpeechSynthesisUtterance {
      text: string;
      lang = "";
      constructor(text: string) {
        this.text = text;
      }
    }
    Object.defineProperty(globalThis, "SpeechSynthesisUtterance", {
      value: TestSpeechSynthesisUtterance,
      configurable: true,
    });
  });

  it("dictates into the initial prompt and creates on the send command", async () => {
    cacheProviders([provider], provider.id);
    vi.spyOn(globalThis, "fetch").mockRejectedValue(new TypeError("offline"));
    const onCreate = vi.fn().mockResolvedValue(true);

    const modal = render(
      <NewSessionModal
        open
        onClose={() => {}}
        onCreate={onCreate}
        defaultCwd="/tmp/project"
        projects={[]}
      />,
    );

    const mic = await waitFor(() => {
      const el = modal.container.querySelector<HTMLButtonElement>(
        '.ns-prompt-actions [data-testid="voice-activation"]',
      );
      if (!el) throw new Error("mic not rendered");
      return el;
    });

    act(() => {
      mic.click();
    });
    await waitFor(() => expect(handlers).not.toBeNull());

    const textarea = modal.container.querySelector<HTMLTextAreaElement>(
      `[data-testid="${NEW_SESSION_PROMPT_TESTID}"]`,
    )!;

    speak("prompt hello world");
    await waitFor(() => expect(textarea.value).toBe("hello world"));

    speak("add tests that's all send");
    await waitFor(() => expect(onCreate).toHaveBeenCalledTimes(1));

    // Successful creation clears the draft after handing off its complete buffer.
    await waitFor(() => expect(textarea.value).toBe(""));
    expect(onCreate.mock.calls[0][0].initialPrompt).toBe("hello world add tests");
  });

  it("keeps voice commands out of the chat composer while the modal owns them", async () => {
    cacheProviders([provider], provider.id);
    vi.spyOn(globalThis, "fetch").mockRejectedValue(new TypeError("offline"));
    const appendToChat = vi.fn();
    window.addEventListener("better-agent-voice-append-draft", appendToChat);

    const modal = render(
      <NewSessionModal
        open
        onClose={() => {}}
        onCreate={vi.fn()}
        defaultCwd="/tmp/project"
        projects={[]}
      />,
    );

    const mic = await waitFor(() => {
      const el = modal.container.querySelector<HTMLButtonElement>(
        '.ns-prompt-actions [data-testid="voice-activation"]',
      );
      if (!el) throw new Error("mic not rendered");
      return el;
    });
    act(() => {
      mic.click();
    });
    await waitFor(() => expect(handlers).not.toBeNull());

    speak("prompt hello world");

    const textarea = modal.container.querySelector<HTMLTextAreaElement>(
      `[data-testid="${NEW_SESSION_PROMPT_TESTID}"]`,
    )!;
    await waitFor(() => expect(textarea.value).toBe("hello world"));
    expect(appendToChat).not.toHaveBeenCalled();
    window.removeEventListener("better-agent-voice-append-draft", appendToChat);
  });
});

describe("voice activation exclusivity", () => {
  it("disables the previously listening mic when another one is enabled", async () => {
    const view = render(
      <>
        <VoiceActivation className="first" />
        <VoiceActivation className="second" />
      </>,
    );
    const [first, second] = Array.from(
      view.container.querySelectorAll<HTMLButtonElement>('[data-testid="voice-activation"]'),
    );

    act(() => {
      first.click();
    });
    expect(first.getAttribute("aria-pressed")).toBe("true");

    act(() => {
      second.click();
    });
    await waitFor(() => expect(first.getAttribute("aria-pressed")).toBe("false"));
    expect(second.getAttribute("aria-pressed")).toBe("true");
  });
});

describe("dictationDelta", () => {
  it("returns only the not-yet-appended tail of the cumulative buffer", () => {
    expect(dictationDelta("hello world add tests", "hello world")).toBe("add tests");
    expect(dictationDelta("hello world", "hello world")).toBe("");
    expect(dictationDelta("hello world", "")).toBe("hello world");
    expect(dictationDelta("unrelated text", "hello")).toBe("unrelated text");
  });
});
