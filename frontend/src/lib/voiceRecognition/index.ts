import { Capacitor } from "@capacitor/core";
import type { VoiceRecognizer } from "./types";
import { WebSpeechVoiceRecognizer } from "./webSpeech";
import { CapacitorVoiceRecognizer } from "./capacitor";

/**
 * Pick the recognizer for the current platform.
 *
 * The Web Speech API is unavailable inside native WebViews (Android System
 * WebView / iOS WKWebView), so on a Capacitor native shell we must use the
 * native plugin instead. The plugin's web fallback is `unimplemented`, so we
 * must not use it on the web — there the browser Web Speech API is the path.
 */
export function createVoiceRecognizer(): VoiceRecognizer {
  if (Capacitor.isNativePlatform()) return new CapacitorVoiceRecognizer();
  return new WebSpeechVoiceRecognizer();
}

export type {
  VoiceRecognizer,
  VoiceRecognizerErrorKind,
  VoiceRecognizerHandlers,
  RecognizedUtterance,
} from "./types";
