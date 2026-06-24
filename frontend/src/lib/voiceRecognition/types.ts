/** One finalized spoken utterance, with alternative transcript guesses. */
export interface RecognizedUtterance {
  transcript: string;
  alternatives: string[];
}

/**
 * Why recognition stopped on an error. `permission-denied` and `unavailable`
 * are terminal — the recognizer stops retrying; the UI should prompt the user
 * (enable mic in settings / device unsupported). `error` is transient and the
 * recognizer will keep retrying.
 */
export type VoiceRecognizerErrorKind = "permission-denied" | "unavailable" | "error";

export interface VoiceRecognizerError {
  kind: VoiceRecognizerErrorKind;
}

/** Callbacks the recognizer fires into the UI layer. */
export interface VoiceRecognizerHandlers {
  /** Fired for each finalized utterance. `alternatives[0]` === `transcript`. */
  onResult: (utterance: RecognizedUtterance) => void;
  /** Fired when active listening starts/stops (drives the UI indicator). */
  onListeningChange: (listening: boolean) => void;
  /** Fired on recognition/permission errors; listening is already false. */
  onError: (error: VoiceRecognizerError) => void;
}

/**
 * Platform-agnostic continuous voice recognizer. Owns its own restart loop —
 * `start()` begins continuous listening (re-listens after each utterance),
 * `stop()` halts and prevents restart. The UI only drives start/stop and
 * reacts to handlers; it does not manage reconnection.
 */
export interface VoiceRecognizer {
  /** Synchronous availability hint — when false the UI hides the mic button. */
  readonly available: boolean;
  /** BCP-47 language tag, e.g. "en-US". Apply before `start()`. */
  setLanguage(language: string): void;
  setHandlers(handlers: VoiceRecognizerHandlers): void;
  /** Idempotent: begin continuous listening (no-op if already running). */
  start(): void;
  /** Idempotent: stop listening and cancel any pending restart. */
  stop(): void;
  /** Release all resources (native listeners, timers). */
  dispose(): void;
}
