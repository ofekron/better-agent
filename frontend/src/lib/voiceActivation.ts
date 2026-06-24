export const VOICE_NEW_SESSION_EVENT = "better-agent-voice-new-session";
export const VOICE_SEND_PROMPT_EVENT = "better-agent-voice-send-prompt";
export const VOICE_APPEND_DRAFT_EVENT = "better-agent-voice-append-draft";
export const VOICE_OPEN_PROMPT_EVENT = "better-agent-voice-open-prompt";

export interface VoicePromptEventDetail {
  text: string;
}

export interface VoiceCommandState {
  mode: "idle" | "prompt" | "confirm-close";
  buffer: string;
}

export type VoiceCommandAction =
  | { type: "new-session" }
  | { type: "open-prompt" }
  | { type: "send-prompt"; text: string }
  | { type: "append-draft"; text: string }
  | { type: "speak"; text: string };

export interface VoiceCommandParseResult {
  state: VoiceCommandState;
  actions: VoiceCommandAction[];
}

const NEW_SESSION_PATTERN = /^\s*(?:new|create|start)\s+(?:a\s+)?session\s*$/i;
const OPEN_PROMPT_PATTERN = /^\s*open\s+prompt\s*$/i;
const LEADING_WORD_PATTERN = /^\s*([a-z]+)\b[:,\s-]*/i;
const SEND_PATTERN = /\b(?:that(?:'|’)?s|that\s+is)\s+all\s*,?\s*(?:send|sent|submit)\b/i;
const CLOSE_PROMPT_PATTERN = /^\s*close\s+prompt\s*$/i;
const YES_PATTERN = /^\s*(?:yes|yeah|yep|send|submit)\s*$/i;
const NO_PATTERN = /^\s*(?:no|nope)\s*$/i;

const IDLE_STATE: VoiceCommandState = { mode: "idle", buffer: "" };

function cleanPromptText(text: string): string {
  return text.replace(/\s+/g, " ").trim();
}

function commandTokenKey(token: string): string {
  return token
    .toLowerCase()
    .replace(/s$/, "")
    .replace(/^b/, "p")
    .replace(/^pr/, "p")
    .replace(/[aeiou]/g, "")
    .replace(/[dt]$/, "t");
}

function isPromptTriggerToken(token: string): boolean {
  return commandTokenKey(token) === commandTokenKey("prompt");
}

function consumePromptText(
  transcript: string,
  prior: string,
): { state: VoiceCommandState; actions: VoiceCommandAction[] } {
  if (CLOSE_PROMPT_PATTERN.test(transcript)) {
    return {
      state: { mode: "confirm-close", buffer: cleanPromptText(prior) },
      actions: [{ type: "speak", text: "I'm done" }],
    };
  }

  const sendMatch = SEND_PATTERN.exec(transcript);
  if (!sendMatch) {
    const text = cleanPromptText(transcript);
    const next = cleanPromptText([prior, text].filter(Boolean).join(" "));
    return {
      state: { mode: "prompt", buffer: next },
      actions: text ? [{ type: "append-draft", text }] : [],
    };
  }

  const beforeSend = transcript.slice(0, sendMatch.index);
  const text = cleanPromptText([prior, beforeSend].filter(Boolean).join(" "));
  return {
    state: IDLE_STATE,
    actions: text ? [{ type: "send-prompt", text }] : [],
  };
}

export function parseVoiceCommand(
  transcript: string,
  state: VoiceCommandState,
): VoiceCommandParseResult {
  const clean = transcript.trim();
  if (!clean) return { state, actions: [] };

  if (OPEN_PROMPT_PATTERN.test(clean)) {
    return {
      state: {
        mode: "prompt",
        buffer: state.mode === "idle" ? "" : cleanPromptText(state.buffer),
      },
      actions: [{ type: "open-prompt" }],
    };
  }

  if (state.mode === "prompt") {
    const parsed = consumePromptText(clean, state.buffer);
    return {
      state: parsed.state,
      actions: parsed.actions,
    };
  }

  if (state.mode === "confirm-close") {
    if (YES_PATTERN.test(clean)) {
      const text = cleanPromptText(state.buffer);
      return {
        state: IDLE_STATE,
        actions: text ? [{ type: "send-prompt", text }] : [],
      };
    }
    if (NO_PATTERN.test(clean)) {
      const text = "close prompt";
      return {
        state: {
          mode: "prompt",
          buffer: cleanPromptText([state.buffer, text].filter(Boolean).join(" ")),
        },
        actions: [{ type: "append-draft", text }],
      };
    }
    return {
      state: {
        mode: "prompt",
        buffer: cleanPromptText([state.buffer, clean].filter(Boolean).join(" ")),
      },
      actions: [{ type: "append-draft", text: clean }],
    };
  }

  if (NEW_SESSION_PATTERN.test(clean)) {
    return { state: IDLE_STATE, actions: [{ type: "new-session" }] };
  }

  const leadingWordMatch = LEADING_WORD_PATTERN.exec(clean);
  if (leadingWordMatch && isPromptTriggerToken(leadingWordMatch[1])) {
    const tail = clean.slice(leadingWordMatch[0].length);
    const parsed = consumePromptText(tail, "");
    return {
      state: parsed.state,
      actions: parsed.actions,
    };
  }

  return { state: IDLE_STATE, actions: [] };
}

export function dispatchVoiceAction(action: VoiceCommandAction) {
  if (action.type === "new-session") {
    window.dispatchEvent(new CustomEvent(VOICE_NEW_SESSION_EVENT));
    return;
  }

  if (action.type === "open-prompt") {
    window.dispatchEvent(new CustomEvent(VOICE_OPEN_PROMPT_EVENT));
    return;
  }

  if (action.type === "append-draft") {
    window.dispatchEvent(
      new CustomEvent<VoicePromptEventDetail>(VOICE_APPEND_DRAFT_EVENT, {
        detail: { text: action.text },
      }),
    );
    return;
  }

  if (action.type === "speak") {
    const synth = window.speechSynthesis;
    if (!synth) return;
    synth.cancel();
    const utterance = new SpeechSynthesisUtterance(action.text);
    utterance.lang = navigator.language || "en-US";
    synth.speak(utterance);
    return;
  }

  window.dispatchEvent(
    new CustomEvent<VoicePromptEventDetail>(VOICE_SEND_PROMPT_EVENT, {
      detail: { text: action.text },
    }),
  );
}

export function voiceRecognitionLanguage(deviceLanguage: string): string {
  const language = deviceLanguage.trim();
  if (/^en(?:-|$)/i.test(language)) return language;
  return "en-US";
}
