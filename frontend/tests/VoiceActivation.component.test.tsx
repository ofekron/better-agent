import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { VoiceActivation } from "../src/components/VoiceActivation";
import type { VoiceRecognizer, VoiceRecognizerHandlers } from "../src/lib/voiceRecognition";

const recognizer = {
  available: true,
  setLanguage: vi.fn(),
  setHandlers: vi.fn(),
  start: vi.fn(),
  stop: vi.fn(),
  dispose: vi.fn(),
} satisfies VoiceRecognizer;

const speechSynthesisMock = {
  cancel: vi.fn(),
  speak: vi.fn(),
};

const t = (key: string) => key;

vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t,
  }),
}));

vi.mock("../src/lib/voiceRecognition", () => ({
  createVoiceRecognizer: () => recognizer,
}));

describe("VoiceActivation", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    Object.defineProperty(document, "visibilityState", {
      value: "visible",
      configurable: true,
    });
    Object.defineProperty(window, "speechSynthesis", {
      value: speechSynthesisMock,
      configurable: true,
    });
    class TestSpeechSynthesisUtterance {
      text: string;
      lang = "";
      constructor(text: string) {
        this.text = text;
      }
    }
    Object.defineProperty(window, "SpeechSynthesisUtterance", {
      value: TestSpeechSynthesisUtterance,
      configurable: true,
    });
    Object.defineProperty(globalThis, "SpeechSynthesisUtterance", {
      value: TestSpeechSynthesisUtterance,
      configurable: true,
    });
  });

  it("starts listening immediately while the activation hint is spoken", () => {
    render(<VoiceActivation />);

    fireEvent.click(screen.getByTestId("voice-activation"));

    expect(recognizer.setHandlers).toHaveBeenCalledWith({
      onResult: expect.any(Function),
      onListeningChange: expect.any(Function),
      onError: expect.any(Function),
    } satisfies VoiceRecognizerHandlers);
    expect(recognizer.start).toHaveBeenCalled();
    expect(speechSynthesisMock.speak).toHaveBeenCalledTimes(1);
    expect(recognizer.start.mock.invocationCallOrder[0]).toBeLessThan(
      speechSynthesisMock.speak.mock.invocationCallOrder[0],
    );
    expect(screen.getByRole("status").textContent).toBe("voice.activationHint");
  });
});
