import { describe, beforeEach, afterEach, it, expect, vi } from "vitest";

import { WebSpeechVoiceRecognizer } from "../src/lib/voiceRecognition/webSpeech";
import type { RecognizedUtterance, VoiceRecognizerError } from "../src/lib/voiceRecognition/types";

/**
 * Fake `SpeechRecognition` instance the recognizer builds around. Captures the
 * config the recognizer writes and the handlers it attaches, and lets each test
 * drive those handlers as the browser would.
 */
interface FakeRecognition {
  continuous: boolean;
  interimResults: boolean;
  lang: string;
  maxAlternatives: number;
  onresult: ((event: unknown) => void) | null;
  onend: (() => void) | null;
  onerror: ((event: unknown) => void) | null;
  startCalls: number;
  abortCalls: number;
  start: () => void;
  abort: (() => void) | undefined;
}

interface FakeCtor {
  instances: FakeRecognition[];
  new (): FakeRecognition;
}

/** Build a constructor whose instances record every interaction. */
function makeCtor(opts: { throwOnSecondStart?: boolean; omitAbort?: boolean } = {}): FakeCtor {
  const instances: FakeRecognition[] = [];
  function Ctor(this: FakeRecognition) {
    this.continuous = false;
    this.interimResults = false;
    this.lang = "";
    this.maxAlternatives = 0;
    this.onresult = null;
    this.onend = null;
    this.onerror = null;
    this.startCalls = 0;
    this.abortCalls = 0;
    this.start = () => {
      this.startCalls += 1;
      if (opts.throwOnSecondStart && this.startCalls === 2) {
        throw new DOMException("already started", "InvalidStateError");
      }
    };
    this.abort = opts.omitAbort ? undefined : () => {
      this.abortCalls += 1;
    };
    instances.push(this);
  }
  // Attach the capture list so tests can read `Ctor.instances`.
  (Ctor as unknown as FakeCtor).instances = instances;
  return Ctor as unknown as FakeCtor;
}

/** Build a results event shaped like the browser's SpeechRecognitionEvent. */
function resultEvent(results: Array<{ isFinal: boolean; alternatives: string[] } | undefined>, resultIndex = 0) {
  return {
    resultIndex,
    results: {
      length: results.length,
      // The recognizer indexes numerically; emulate the live results list.
      ...(Object.fromEntries(
        results.map((r, i) => [
          i,
          r
            ? {
                isFinal: r.isFinal,
                length: r.alternatives.length,
                ...Object.fromEntries(r.alternatives.map((t, a) => [a, { transcript: t }])),
              }
            : undefined,
        ]),
      ) as Record<number, unknown>),
    },
  };
}

const HANDLERS = () => ({
  onResult: vi.fn<(utterance: RecognizedUtterance) => void>(),
  onListeningChange: vi.fn<(listening: boolean) => void>(),
  onError: vi.fn<(error: VoiceRecognizerError) => void>(),
});

describe("WebSpeechVoiceRecognizer", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
    delete (window as unknown as Record<string, unknown>).SpeechRecognition;
    delete (window as unknown as Record<string, unknown>).webkitSpeechRecognition;
  });

  describe("availability and constructor", () => {
    it("is unavailable and start() is a no-op when neither SpeechRecognition ctor exists", () => {
      const recognizer = new WebSpeechVoiceRecognizer();
      expect(recognizer.available).toBe(false);

      const handlers = HANDLERS();
      recognizer.setHandlers(handlers);
      recognizer.start(); // no Ctor → early return, nothing constructed

      expect(handlers.onListeningChange).not.toHaveBeenCalled();
    });

    it("prefers standard SpeechRecognition over the webkit fallback", () => {
      const standard = makeCtor();
      const webkit = makeCtor();
      (window as unknown as Record<string, unknown>).SpeechRecognition = standard;
      (window as unknown as Record<string, unknown>).webkitSpeechRecognition = webkit;

      const recognizer = new WebSpeechVoiceRecognizer();
      expect(recognizer.available).toBe(true);
      recognizer.start();

      expect(standard.instances).toHaveLength(1);
      expect(webkit.instances).toHaveLength(0);
    });

    it("falls back to webkitSpeechRecognition when the standard ctor is absent", () => {
      const webkit = makeCtor();
      (window as unknown as Record<string, unknown>).webkitSpeechRecognition = webkit;

      const recognizer = new WebSpeechVoiceRecognizer();
      expect(recognizer.available).toBe(true);
      recognizer.start();

      expect(webkit.instances).toHaveLength(1);
    });
  });

  describe("start()", () => {
    it("configures and starts the recognition instance and announces listening", () => {
      const Ctor = makeCtor();
      (window as unknown as Record<string, unknown>).SpeechRecognition = Ctor;

      const recognizer = new WebSpeechVoiceRecognizer();
      const handlers = HANDLERS();
      recognizer.setHandlers(handlers);
      recognizer.setLanguage("he-IL");
      recognizer.start();

      const recognition = Ctor.instances[0];
      expect(recognition.continuous).toBe(true);
      expect(recognition.interimResults).toBe(false);
      expect(recognition.maxAlternatives).toBe(5);
      expect(recognition.lang).toBe("he-IL");
      expect(recognition.startCalls).toBe(1);
      expect(handlers.onListeningChange).toHaveBeenCalledWith(true);
    });

    it("treats a thrown start() (already started) as still listening without re-announcing", () => {
      const Ctor = makeCtor({ throwOnSecondStart: true });
      (window as unknown as Record<string, unknown>).SpeechRecognition = Ctor;

      const recognizer = new WebSpeechVoiceRecognizer();
      const handlers = HANDLERS();
      recognizer.setHandlers(handlers);
      recognizer.start();
      expect(() => recognizer.start()).not.toThrow();

      // Only the first start() announced listening; the swallowed throw did not.
      expect(handlers.onListeningChange).toHaveBeenCalledTimes(1);
    });
  });

  describe("onresult", () => {
    function startWithHandlers() {
      const Ctor = makeCtor();
      (window as unknown as Record<string, unknown>).SpeechRecognition = Ctor;
      const recognizer = new WebSpeechVoiceRecognizer();
      const handlers = HANDLERS();
      recognizer.setHandlers(handlers);
      recognizer.start();
      return { recognizer, handlers, recognition: Ctor.instances[0] };
    }

    it("emits the first final alternative as the transcript plus all alternatives", () => {
      const { handlers, recognition } = startWithHandlers();
      recognition.onresult!(resultEvent([{ isFinal: true, alternatives: ["hello world", "hello"] }]) as never);

      expect(handlers.onResult).toHaveBeenCalledWith({
        transcript: "hello world",
        alternatives: ["hello world", "hello"],
      });
    });

    it("skips non-final results", () => {
      const { handlers, recognition } = startWithHandlers();
      recognition.onresult!(resultEvent([{ isFinal: false, alternatives: ["partial"] }]) as never);

      expect(handlers.onResult).not.toHaveBeenCalled();
    });

    it("does not emit when every alternative is empty after trimming", () => {
      const { handlers, recognition } = startWithHandlers();
      recognition.onresult!(resultEvent([{ isFinal: true, alternatives: ["   ", ""] }]) as never);

      expect(handlers.onResult).not.toHaveBeenCalled();
    });

    it("starts iterating at resultIndex, ignoring earlier entries", () => {
      const { handlers, recognition } = startWithHandlers();
      recognition.onresult!(
        resultEvent(
          [{ isFinal: true, alternatives: ["ignored"] }, { isFinal: true, alternatives: ["kept"] }],
          1,
        ) as never,
      );

      expect(handlers.onResult).toHaveBeenCalledWith({ transcript: "kept", alternatives: ["kept"] });
    });

    it("ignores an undefined result entry without throwing", () => {
      const { handlers, recognition } = startWithHandlers();
      recognition.onresult!(resultEvent([undefined, { isFinal: true, alternatives: ["kept"] }], 0) as never);

      expect(handlers.onResult).toHaveBeenCalledWith({ transcript: "kept", alternatives: ["kept"] });
    });

    it("drops alternatives whose transcript is absent (nullish default), keeping the rest", () => {
      const { handlers, recognition } = startWithHandlers();
      recognition.onresult!(
        resultEvent([{ isFinal: true, alternatives: ["hello", undefined as unknown as string, "world"] }]) as never,
      );

      expect(handlers.onResult).toHaveBeenCalledWith({
        transcript: "hello",
        alternatives: ["hello", "world"],
      });
    });
  });

  describe("onerror", () => {
    function startWithHandlers() {
      const Ctor = makeCtor();
      (window as unknown as Record<string, unknown>).SpeechRecognition = Ctor;
      const recognizer = new WebSpeechVoiceRecognizer();
      const handlers = HANDLERS();
      recognizer.setHandlers(handlers);
      recognizer.start();
      return { recognizer, handlers, recognition: Ctor.instances[0] };
    }

    it("reports a transient error and keeps the restart loop armed", () => {
      const { handlers, recognition } = startWithHandlers();
      recognition.onerror!({ error: "network" });

      expect(handlers.onListeningChange).toHaveBeenCalledWith(false);
      expect(handlers.onError).toHaveBeenCalledWith({ kind: "error" });

      // Transient: a later onend still schedules a restart.
      recognition.onend!();
      expect(handlers.onListeningChange).toHaveBeenCalledWith(false);
      vi.advanceTimersByTime(250);
      expect(recognition.startCalls).toBe(2);
    });

    it("treats not-allowed as a terminal permission denial and prevents restart", () => {
      const { handlers, recognition } = startWithHandlers();
      recognition.onerror!({ error: "not-allowed" });

      expect(handlers.onError).toHaveBeenCalledWith({ kind: "permission-denied" });

      recognition.onend!(); // running was cleared → no restart scheduled
      vi.advanceTimersByTime(250);
      expect(recognition.startCalls).toBe(1);
    });

    it("treats service-not-allowed as a terminal permission denial too", () => {
      const { handlers, recognition } = startWithHandlers();
      recognition.onerror!({ error: "service-not-allowed" });

      expect(handlers.onError).toHaveBeenCalledWith({ kind: "permission-denied" });
    });
  });

  describe("onend restart loop", () => {
    it("restarts listening after the configured delay", () => {
      const Ctor = makeCtor();
      (window as unknown as Record<string, unknown>).SpeechRecognition = Ctor;
      const recognizer = new WebSpeechVoiceRecognizer();
      const handlers = HANDLERS();
      recognizer.setHandlers(handlers);
      recognizer.start();

      const recognition = Ctor.instances[0];
      recognition.onend!();
      expect(handlers.onListeningChange).toHaveBeenCalledWith(false);

      vi.advanceTimersByTime(250);
      expect(recognition.startCalls).toBe(2);
      expect(handlers.onListeningChange).toHaveBeenCalledWith(true);
    });
  });

  describe("stop()", () => {
    it("aborts the active recognition, clears the pending restart, and announces not-listening", () => {
      const Ctor = makeCtor();
      (window as unknown as Record<string, unknown>).SpeechRecognition = Ctor;
      const recognizer = new WebSpeechVoiceRecognizer();
      const handlers = HANDLERS();
      recognizer.setHandlers(handlers);
      recognizer.start();
      const recognition = Ctor.instances[0];

      recognition.onend!(); // arm a restart
      recognizer.stop(); // must cancel it
      vi.advanceTimersByTime(250);

      expect(recognition.abortCalls).toBe(1);
      expect(recognition.startCalls).toBe(1); // restart timer was cleared
      expect(handlers.onListeningChange).toHaveBeenCalledWith(false);
    });

    it("is a safe no-op before listening ever starts (no instance, no duplicate announce)", () => {
      const Ctor = makeCtor();
      (window as unknown as Record<string, unknown>).SpeechRecognition = Ctor;
      const recognizer = new WebSpeechVoiceRecognizer();
      const handlers = HANDLERS();
      recognizer.setHandlers(handlers);

      expect(() => recognizer.stop()).not.toThrow();
      expect(handlers.onListeningChange).not.toHaveBeenCalled();
    });

    it("does not re-announce not-listening when already stopped (dedup)", () => {
      const Ctor = makeCtor();
      (window as unknown as Record<string, unknown>).SpeechRecognition = Ctor;
      const recognizer = new WebSpeechVoiceRecognizer();
      const handlers = HANDLERS();
      recognizer.setHandlers(handlers);
      recognizer.start();
      recognizer.stop();
      recognizer.stop(); // listening already false → dedup

      expect(handlers.onListeningChange).toHaveBeenCalledTimes(2); // true once, false once
    });

    it("tolerates a recognition instance without an abort method", () => {
      const Ctor = makeCtor({ omitAbort: true });
      (window as unknown as Record<string, unknown>).SpeechRecognition = Ctor;
      const recognizer = new WebSpeechVoiceRecognizer();
      recognizer.setHandlers(HANDLERS());
      recognizer.start();

      expect(() => recognizer.stop()).not.toThrow();
    });
  });

  describe("default handlers", () => {
    it("drives safely before setHandlers is attached (module default no-ops)", () => {
      const Ctor = makeCtor();
      (window as unknown as Record<string, unknown>).SpeechRecognition = Ctor;
      const recognizer = new WebSpeechVoiceRecognizer();
      recognizer.start(); // no setHandlers — uses the built-in NO_HANDLERS default
      const recognition = Ctor.instances[0];

      // start() announces listening through the default onListeningChange.
      expect(() => {
        recognition.onresult!(resultEvent([{ isFinal: true, alternatives: ["hi"] }]) as never);
        recognition.onerror!({ error: "network" });
      }).not.toThrow();
    });
  });

  describe("beginListening guard", () => {
    it("skips a scheduled restart if a permission error disarmed the loop meanwhile", () => {
      const Ctor = makeCtor();
      (window as unknown as Record<string, unknown>).SpeechRecognition = Ctor;
      const recognizer = new WebSpeechVoiceRecognizer();
      recognizer.setHandlers(HANDLERS());
      recognizer.start();
      const recognition = Ctor.instances[0];

      recognition.onend!(); // schedule restart timer (running still true)
      recognition.onerror!({ error: "not-allowed" }); // disarm loop, timer left pending
      vi.advanceTimersByTime(250); // pending restart fires beginListening, which no-ops

      expect(recognition.startCalls).toBe(1);
    });
  });

  describe("dispose()", () => {
    it("stops, detaches handlers, aborts again, and drops the instance", () => {
      const Ctor = makeCtor();
      (window as unknown as Record<string, unknown>).SpeechRecognition = Ctor;
      const recognizer = new WebSpeechVoiceRecognizer();
      const handlers = HANDLERS();
      recognizer.setHandlers(handlers);
      recognizer.start();
      const recognition = Ctor.instances[0];

      recognizer.dispose();

      expect(recognition.abortCalls).toBe(2); // stop() + dispose()
      expect(recognition.onresult).toBeNull();
      expect(recognition.onend).toBeNull();
      expect(recognition.onerror).toBeNull();
      // handlers were reset to no-ops: a stray event does not reach the test spies.
      expect(handlers.onResult).not.toHaveBeenCalled();
    });

    it("is idempotent", () => {
      const Ctor = makeCtor();
      (window as unknown as Record<string, unknown>).SpeechRecognition = Ctor;
      const recognizer = new WebSpeechVoiceRecognizer();
      recognizer.setHandlers(HANDLERS());
      recognizer.start();

      expect(() => {
        recognizer.dispose();
        recognizer.dispose();
      }).not.toThrow();
    });
  });
});
