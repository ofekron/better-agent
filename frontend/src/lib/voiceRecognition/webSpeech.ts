import type {
  VoiceRecognizer,
  VoiceRecognizerErrorKind,
  VoiceRecognizerHandlers,
} from "./types";

interface SpeechRecognitionAlternativeLike {
  transcript: string;
}

interface SpeechRecognitionResultLike {
  isFinal: boolean;
  length: number;
  [index: number]: SpeechRecognitionAlternativeLike;
}

interface SpeechRecognitionEventLike {
  resultIndex: number;
  results: {
    length: number;
    [index: number]: SpeechRecognitionResultLike;
  };
}

interface SpeechRecognitionErrorEventLike {
  error: string;
}

interface SpeechRecognitionLike {
  continuous: boolean;
  interimResults: boolean;
  lang: string;
  maxAlternatives: number;
  onresult: ((event: SpeechRecognitionEventLike) => void) | null;
  onend: (() => void) | null;
  onerror: ((event: SpeechRecognitionErrorEventLike) => void) | null;
  start: () => void;
  abort?: () => void;
}

/** Web Speech `error` values that mean the user must grant mic/speech access. */
const PERMISSION_ERROR_CODES = new Set(["not-allowed", "service-not-allowed"]);

type SpeechRecognitionConstructor = new () => SpeechRecognitionLike;

type SpeechWindow = Window & {
  SpeechRecognition?: SpeechRecognitionConstructor;
  webkitSpeechRecognition?: SpeechRecognitionConstructor;
};

const NO_HANDLERS: VoiceRecognizerHandlers = {
  onResult: () => {},
  onListeningChange: () => {},
  onError: () => {},
};

const RESTART_DELAY_MS = 250;

/**
 * Continuous recognizer backed by the browser Web Speech API
 * (`SpeechRecognition` / `webkitSpeechRecognition`). Mirrors the prior
 * in-component behavior: continuous, final-only results, 250ms restart on end.
 */
export class WebSpeechVoiceRecognizer implements VoiceRecognizer {
  readonly available: boolean;
  private readonly Ctor: SpeechRecognitionConstructor | null;
  private recognition: SpeechRecognitionLike | null = null;
  private language = "en-US";
  private handlers: VoiceRecognizerHandlers = NO_HANDLERS;
  private running = false;
  private restartTimer: ReturnType<typeof setTimeout> | null = null;

  constructor() {
    const speechWindow = window as SpeechWindow;
    this.Ctor = speechWindow.SpeechRecognition ?? speechWindow.webkitSpeechRecognition ?? null;
    this.available = this.Ctor !== null;
  }

  setLanguage(language: string) {
    this.language = language;
  }

  setHandlers(handlers: VoiceRecognizerHandlers) {
    this.handlers = handlers;
  }

  start() {
    if (!this.Ctor) return;
    this.running = true;
    this.beginListening();
  }

  stop() {
    this.running = false;
    this.clearRestartTimer();
    this.recognition?.abort?.();
    this.emitListening(false);
  }

  dispose() {
    this.stop();
    const recognition = this.recognition;
    if (recognition) {
      recognition.onresult = null;
      recognition.onend = null;
      recognition.onerror = null;
      recognition.abort?.();
    }
    this.recognition = null;
    this.handlers = NO_HANDLERS;
  }

  private beginListening() {
    if (!this.running) return;
    const recognition = this.ensureRecognition();
    recognition.lang = this.language;
    try {
      recognition.start();
      this.emitListening(true);
    } catch {
      // start() throws if already started — treat as still listening.
    }
  }

  private ensureRecognition(): SpeechRecognitionLike {
    if (this.recognition) return this.recognition;
    const recognition = new this.Ctor!();
    recognition.continuous = true;
    recognition.interimResults = false;
    recognition.maxAlternatives = 5;
    recognition.onresult = (event) => {
      for (let index = event.resultIndex; index < event.results.length; index += 1) {
        const result = event.results[index];
        if (!result?.isFinal) continue;
        const alternatives = Array.from({ length: result.length }, (_, altIndex) =>
          result[altIndex]?.transcript?.trim() ?? "",
        ).filter(Boolean);
        const transcript = alternatives[0] ?? "";
        if (transcript) this.handlers.onResult({ transcript, alternatives });
      }
    };
    recognition.onerror = (event) => {
      this.emitListening(false);
      const kind: VoiceRecognizerErrorKind = PERMISSION_ERROR_CODES.has(event?.error)
        ? "permission-denied"
        : "error";
      // Permission denial needs user action — stop so onend won't auto-restart.
      if (kind === "permission-denied") this.running = false;
      this.handlers.onError({ kind });
    };
    recognition.onend = () => {
      this.emitListening(false);
      if (!this.running) return;
      this.restartTimer = setTimeout(() => this.beginListening(), RESTART_DELAY_MS);
    };
    this.recognition = recognition;
    return recognition;
  }

  private clearRestartTimer() {
    if (!this.restartTimer) return;
    clearTimeout(this.restartTimer);
    this.restartTimer = null;
  }

  private emitListening(listening: boolean) {
    if (this.listening === listening) return;
    this.listening = listening;
    this.handlers.onListeningChange(listening);
  }

  private listening = false;
}
