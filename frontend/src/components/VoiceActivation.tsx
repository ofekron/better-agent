import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import Icon from "./Icon";
import {
  dispatchVoiceAction,
  parseVoiceCommand,
  type VoiceCommandState,
  voiceRecognitionLanguage,
} from "../lib/voiceActivation";
import {
  createVoiceRecognizer,
  type VoiceRecognizerErrorKind,
} from "../lib/voiceRecognition";
import { API } from "../api";

function promptTextareaIsFocused() {
  return document.activeElement instanceof HTMLTextAreaElement
    && document.activeElement.dataset.testid === "input-textarea";
}

const HINT_AUTO_DISMISS_MS = 4000;
const VOICE_INTRO_DISMISS_MS = 7000;

type VoiceHintKind = "info" | "error";

export function VoiceActivation({
  onEnabledChange,
}: {
  onEnabledChange?: (enabled: boolean) => void;
}) {
  const { t } = useTranslation();
  const [recognizer] = useState(() => createVoiceRecognizer());
  const [enabled, setEnabled] = useState(false);
  const [listening, setListening] = useState(false);
  const [capturingPrompt, setCapturingPrompt] = useState(false);
  const [hint, setHint] = useState<string | null>(null);
  const [hintKind, setHintKind] = useState<VoiceHintKind>("info");
  const commandStateRef = useRef<VoiceCommandState>({ mode: "idle", buffer: "" });
  const hintTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const closeOnBackgroundRef = useRef(true);

  useEffect(() => {
    fetch(`${API}/api/user-prefs`)
      .then((r) => r.json())
      .then((data: { voice_close_on_background?: unknown }) => {
        if (typeof data.voice_close_on_background === "boolean") {
          closeOnBackgroundRef.current = data.voice_close_on_background;
        }
      })
      .catch(() => {});
  }, []);

  const clearHintTimer = () => {
    if (!hintTimerRef.current) return;
    clearTimeout(hintTimerRef.current);
    hintTimerRef.current = null;
  };

  const hintFor = (kind: VoiceRecognizerErrorKind): string | null => {
    if (kind === "permission-denied") return t("voice.permissionDenied");
    if (kind === "unavailable") return t("voice.unavailable");
    return null;
  };

  useEffect(() => {
    recognizer.setLanguage(voiceRecognitionLanguage(navigator.language || ""));
    if (!enabled) {
      recognizer.stop();
      commandStateRef.current = { mode: "idle", buffer: "" };
      setCapturingPrompt(false);
      return;
    }

    const introText = t("voice.activationHint");
    setHintKind("info");
    setHint(introText);
    clearHintTimer();
    hintTimerRef.current = setTimeout(() => setHint(null), VOICE_INTRO_DISMISS_MS);

    recognizer.setHandlers({
      onResult: ({ transcript, alternatives }) => {
        let parsed = parseVoiceCommand(transcript, commandStateRef.current);
        if (commandStateRef.current.mode === "idle" && parsed.actions.length === 0 && parsed.state.mode === "idle") {
          for (const alternative of alternatives.slice(1)) {
            const alternativeParsed = parseVoiceCommand(alternative, commandStateRef.current);
            if (alternativeParsed.actions.length === 0 && alternativeParsed.state.mode === "idle") continue;
            parsed = alternativeParsed;
            break;
          }
        }
        commandStateRef.current = parsed.state;
        setCapturingPrompt(parsed.state.mode === "prompt");
        const actions = parsed.actions.length > 0
          ? parsed.actions
          : promptTextareaIsFocused()
            ? [{ type: "append-draft" as const, text: transcript.trim() }]
            : [];
        for (const action of actions) {
          dispatchVoiceAction(action);
        }
      },
      onListeningChange: (active) => {
        setListening(active);
      },
      onError: (error) => {
        setListening(false);
        const message = hintFor(error.kind);
        if (!message) return;
        setHintKind("error");
        setHint(message);
        clearHintTimer();
        if (error.kind === "error") {
          hintTimerRef.current = setTimeout(() => setHint(null), HINT_AUTO_DISMISS_MS);
        }
      },
    });

    const onVisibilityChange = () => {
      if (document.visibilityState === "visible") {
        startListening();
        return;
      }
      if (closeOnBackgroundRef.current) {
        setEnabled(false);
        return;
      }
      recognizer.stop();
    };

    const startListening = () => {
      if (document.visibilityState !== "visible") return;
      recognizer.start();
    };

    const speakIntro = () => {
      const synth = window.speechSynthesis;
      if (!synth) return;
      synth.cancel();
      const utterance = new SpeechSynthesisUtterance(introText);
      utterance.lang = navigator.language || "en-US";
      synth.speak(utterance);
    };

    document.addEventListener("visibilitychange", onVisibilityChange);
    startListening();
    speakIntro();

    return () => {
      document.removeEventListener("visibilitychange", onVisibilityChange);
      recognizer.stop();
      window.speechSynthesis?.cancel();
    };
  }, [enabled, recognizer, t]);

  useEffect(() => {
    onEnabledChange?.(enabled);
  }, [enabled, onEnabledChange]);

  // Release native listeners/timers on unmount.
  useEffect(() => () => {
    clearHintTimer();
    recognizer.dispose();
  }, [recognizer]);

  if (!recognizer.available) return null;

  const active = enabled && listening;
  const label = enabled ? t("voice.disable") : t("voice.enable");
  const toggleEnabled = () => {
    setEnabled((value) => {
      const next = !value;
      if (!next) {
        commandStateRef.current = { mode: "idle", buffer: "" };
        setListening(false);
        setCapturingPrompt(false);
        setHint(null);
        setHintKind("info");
        clearHintTimer();
      }
      return next;
    });
  };

  return (
    <span className="voice-activation-wrap">
      <button
        type="button"
        className={`voice-activation${active ? " listening" : ""}${capturingPrompt ? " capturing" : ""}${hint && hintKind === "error" ? " error" : ""}`}
        onClick={toggleEnabled}
        aria-pressed={enabled}
        aria-label={hint ?? label}
        title={hint ?? label}
        data-testid="voice-activation"
      >
        <Icon name="mic" size={20} />
      </button>
      {hint && <span className="voice-hint" role="status">{hint}</span>}
    </span>
  );
}
