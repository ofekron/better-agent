import { SpeechRecognition } from "@capacitor-community/speech-recognition";
import type {
  VoiceRecognizer,
  VoiceRecognizerErrorKind,
  VoiceRecognizerHandlers,
} from "./types";

const NO_HANDLERS: VoiceRecognizerHandlers = {
  onResult: () => {},
  onListeningChange: () => {},
  onError: () => {},
};

const RESTART_DELAY_MS = 150;
const ERROR_BACKOFF_MS = 500;

/** Terminal errors — the loop stops retrying and the UI must intervene. */
const TERMINAL_ERRORS: ReadonlySet<VoiceRecognizerErrorKind> = new Set([
  "permission-denied",
  "unavailable",
]);

class RecognizerError extends Error {
  readonly kind: VoiceRecognizerErrorKind;

  constructor(kind: VoiceRecognizerErrorKind, message: string) {
    super(message);
    this.kind = kind;
  }
}

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/**
 * Continuous recognizer backed by the native Android/iOS speech recognizer via
 * the `@capacitor-community/speech-recognition` Capacitor plugin.
 *
 * The plugin's `start({ partialResults: false })` resolves once per utterance,
 * so continuity is driven by a promise loop rather than recognition events —
 * this avoids depending on cross-platform event-timing quirks.
 */
export class CapacitorVoiceRecognizer implements VoiceRecognizer {
  readonly available = true;
  private language = "en-US";
  private handlers: VoiceRecognizerHandlers = NO_HANDLERS;
  private running = false;
  private permissionGranted = false;
  private listening = false;
  private loopRunning = false;

  setLanguage(language: string) {
    this.language = language;
  }

  setHandlers(handlers: VoiceRecognizerHandlers) {
    this.handlers = handlers;
  }

  start() {
    this.running = true;
    if (this.loopRunning) return;
    void this.loop();
  }

  stop() {
    this.running = false;
    void SpeechRecognition.stop().catch(() => {});
    this.emitListening(false);
  }

  dispose() {
    this.stop();
    void SpeechRecognition.removeAllListeners().catch(() => {});
    this.handlers = NO_HANDLERS;
  }

  private async loop() {
    this.loopRunning = true;
    try {
      while (this.running) {
        try {
          await this.ensureAvailable();
          await this.ensurePermission();
          this.emitListening(true);
          const { matches } = await SpeechRecognition.start({
            language: this.language,
            maxResults: 5,
            partialResults: false,
            popup: false,
          });
          this.emitListening(false);
          const alternatives = (matches ?? []).map((m) => m.trim()).filter(Boolean);
          const transcript = alternatives[0] ?? "";
          if (transcript) this.handlers.onResult({ transcript, alternatives });
        } catch (raw) {
          this.emitListening(false);
          const kind: VoiceRecognizerErrorKind =
            raw instanceof RecognizerError ? raw.kind : "error";
          this.handlers.onError({ kind });
          // Terminal errors (denied / unsupported) need user action — stop.
          if (TERMINAL_ERRORS.has(kind)) {
            this.running = false;
            break;
          }
          if (!this.running) break;
          await delay(ERROR_BACKOFF_MS);
          continue;
        }
        if (!this.running) break;
        await delay(RESTART_DELAY_MS);
      }
    } finally {
      this.loopRunning = false;
      // If re-enabled while the loop was unwinding, resume immediately.
      if (this.running) void this.loop();
    }
  }

  private async ensureAvailable() {
    const { available } = await SpeechRecognition.available();
    if (!available) throw new RecognizerError("unavailable", "speech recognition unavailable");
  }

  // `granted` is cached; once the OS records a decision it won't re-prompt, so
  // denied states short-circuit without a request and the UI tells the user to
  // open Settings (toggling the mic off→on re-checks after they fix it).
  private async ensurePermission() {
    if (this.permissionGranted) return;
    const current = await SpeechRecognition.checkPermissions();
    if (current.speechRecognition === "granted") {
      this.permissionGranted = true;
      return;
    }
    if (current.speechRecognition === "denied") {
      throw new RecognizerError("permission-denied", "speech recognition permission denied");
    }
    const requested = await SpeechRecognition.requestPermissions();
    if (requested.speechRecognition === "granted") {
      this.permissionGranted = true;
      return;
    }
    throw new RecognizerError("permission-denied", "speech recognition permission denied");
  }

  private emitListening(listening: boolean) {
    if (this.listening === listening) return;
    this.listening = listening;
    this.handlers.onListeningChange(listening);
  }
}
