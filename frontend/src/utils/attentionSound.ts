import { useEffect } from "react";
import { eventBus } from "../lib/eventBus";

// A short two-tone "ding" synthesized via the Web Audio API so we ship no
// audio asset. Played when an extension attention marker that declares
// `sound: true` lands on a session (e.g. the needs-user-decision extension
// firing on a turn that needs the user).
//
// Browsers suspend AudioContext until a user gesture; we lazily create +
// resume it on the first marker. If playback is blocked (no gesture yet,
// or audio disabled), the call silently no-ops — never throws.

let ctx: AudioContext | null = null;

function audioCtx(): AudioContext | null {
  if (typeof window === "undefined") return null;
  const Ctor: typeof AudioContext | undefined =
    window.AudioContext
    || (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
  if (!Ctor) return null;
  if (!ctx) ctx = new Ctor();
  return ctx;
}

export function playAttentionSound(): void {
  const ac = audioCtx();
  if (!ac) return;
  if (ac.state === "suspended") {
    // Resume is async; best-effort — if it hasn't unlocked, this ding is
    // dropped (acceptable: there will be another marker soon enough).
    ac.resume().catch(() => {});
  }
  const now = ac.currentTime;
  const tones = [
    { f: 880, t: 0 },
    { f: 1320, t: 0.12 },
  ];
  for (const { f, t } of tones) {
    const osc = ac.createOscillator();
    const gain = ac.createGain();
    osc.type = "sine";
    osc.frequency.value = f;
    // Short envelope: attack to 0.18, decay to 0 by 0.25s.
    gain.gain.setValueAtTime(0, now + t);
    gain.gain.linearRampToValueAtTime(0.18, now + t + 0.015);
    gain.gain.exponentialRampToValueAtTime(0.0001, now + t + 0.25);
    osc.connect(gain);
    gain.connect(ac.destination);
    osc.start(now + t);
    osc.stop(now + t + 0.27);
  }
}

/** Subscribe to extension attention markers and play the attention sound
 *  for any that declare `sound: true`. Live WS deltas only — the bootstrap
 *  snapshot does not route through this event, so loading the app never
 *  triggers a burst of sounds. */
export function useAttentionSound(): void {
  useEffect(() => {
    return eventBus.subscribe("session_marker_changed", (p) => {
      if (p?.marker?.sound) playAttentionSound();
    });
  }, []);
}
