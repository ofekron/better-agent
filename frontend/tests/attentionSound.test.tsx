/**
 * Unit tests for `src/utils/attentionSound.ts`.
 *
 * Covers the two exported behaviors:
 *  - `playAttentionSound`: synthesizes a two-tone "ding" through the Web
 *    Audio API, lazily creating + resuming the AudioContext, and no-ops
 *    harmlessly when no AudioContext is available.
 *  - `useAttentionSound`: subscribes to `session_marker_changed` on the
 *    event bus and plays the ding for markers that declare `sound: true`,
 *    gated by an optional extension app setting (`sound_setting`).
 *
 * The AudioContext and the extension-settings reader are faked; the event
 * bus is the real module (re-imported alongside the SUT per test so the
 * module-level AudioContext cache and bus singleton don't leak between
 * cases). This is the unit tier, where faking browser/audio APIs is
 * appropriate; the full-stack tier exercises the live wiring.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { renderHook } from "@testing-library/react";

// Controllable extension-setting gate. The hook mutes only when
// `extensionAppSettingValue(...) === false`; undefined (cache cold) leaves
// the declared default in force, so the default here is "plays".
// `vi.hoisted` is hoisted together with the `vi.mock` factory below, so the
// factory can safely read this mutable cell (a plain outer `let` would be
// shadowed by mock hoisting and read as undefined).
const settingRef = vi.hoisted(() => ({ value: undefined as unknown }));
vi.mock("../src/hooks/useExtensionAppSettings", () => ({
  useExtensionAppSettingsSync: () => {},
  extensionAppSettingValue: () => settingRef.value,
}));

interface FakeCtx {
  state: string;
  currentTime: number;
  resume: ReturnType<typeof vi.fn>;
  destination: symbol;
  createOscillator: ReturnType<typeof vi.fn>;
  createGain: ReturnType<typeof vi.fn>;
}
interface FakeOsc {
  type: string;
  frequency: { value: number };
  connect: ReturnType<typeof vi.fn>;
  start: ReturnType<typeof vi.fn>;
  stop: ReturnType<typeof vi.fn>;
}
interface FakeGain {
  gain: {
    setValueAtTime: ReturnType<typeof vi.fn>;
    linearRampToValueAtTime: ReturnType<typeof vi.fn>;
    exponentialRampToValueAtTime: ReturnType<typeof vi.fn>;
  };
  connect: ReturnType<typeof vi.fn>;
}

/** Build a fake AudioContext instance + a constructor that returns it. */
function makeFakeCtx(state = "running"): {
  Ctor: ReturnType<typeof vi.fn>;
  ctx: FakeCtx;
  oscs: FakeOsc[];
  gains: FakeGain[];
} {
  const oscs: FakeOsc[] = [];
  const gains: FakeGain[] = [];
  const ctx: FakeCtx = {
    state,
    currentTime: 100,
    resume: vi.fn().mockResolvedValue(undefined),
    destination: Symbol("destination"),
    createOscillator: vi.fn(() => {
      const o: FakeOsc = {
        type: "",
        frequency: { value: 0 },
        connect: vi.fn(),
        start: vi.fn(),
        stop: vi.fn(),
      };
      oscs.push(o);
      return o;
    }),
    createGain: vi.fn(() => {
      const g: FakeGain = {
        gain: {
          setValueAtTime: vi.fn(),
          linearRampToValueAtTime: vi.fn(),
          exponentialRampToValueAtTime: vi.fn(),
        },
        connect: vi.fn(),
      };
      gains.push(g);
      return g;
    }),
  };
  // `vi.fn` must wrap a real `function` (not an arrow) so `new Ctor()`
  // constructs and returns the fake instance — arrow mocks are not
  // constructible and silently yield undefined.
  const Ctor = vi.fn(function () {
    return ctx;
  });
  return { Ctor, ctx, oscs, gains };
}

/** Install (or clear) the AudioContext constructor(s) on window. */
function setAudioCtor(Ctor: unknown, webkit?: unknown): void {
  const w = window as unknown as Record<string, unknown>;
  if (Ctor === undefined) delete w.AudioContext;
  else w.AudioContext = Ctor;
  if (webkit === undefined) delete w.webkitAudioContext;
  else w.webkitAudioContext = webkit;
}

interface Fresh {
  playAttentionSound: () => void;
  useAttentionSound: () => void;
  bus: { publish: (type: string, payload: unknown) => void };
}

/** Reset the module registry so the SUT's module-level AudioContext cache
 *  starts empty, and re-import the SUT with the SAME event-bus instance it
 *  subscribes to (otherwise publish/subscribe would land on different
 *  singletons after a reset). */
async function fresh(): Promise<Fresh> {
  vi.resetModules();
  const mod = await import("../src/utils/attentionSound");
  const { eventBus } = await import("../src/lib/eventBus");
  return {
    playAttentionSound: mod.playAttentionSound,
    useAttentionSound: mod.useAttentionSound,
    bus: eventBus,
  };
}

describe("playAttentionSound", () => {
  beforeEach(() => {
    settingRef.value = undefined;
  });
  afterEach(() => {
    setAudioCtor(undefined, undefined);
  });

  it("synthesizes a two-tone sine ding and wires each tone to destination", async () => {
    const { Ctor, ctx, oscs, gains } = makeFakeCtx("running");
    setAudioCtor(Ctor);
    const { playAttentionSound } = await fresh();

    playAttentionSound();

    // One context, two oscillators + two gains (one per tone).
    expect(Ctor).toHaveBeenCalledTimes(1);
    expect(ctx.createOscillator).toHaveBeenCalledTimes(2);
    expect(ctx.createGain).toHaveBeenCalledTimes(2);
    expect(oscs).toHaveLength(2);
    expect(gains).toHaveLength(2);

    // 880Hz then 1320Hz sine tones.
    expect(oscs[0].type).toBe("sine");
    expect(oscs[1].type).toBe("sine");
    expect(oscs[0].frequency.value).toBe(880);
    expect(oscs[1].frequency.value).toBe(1320);

    // Each oscillator feeds its gain, each gain feeds destination.
    expect(oscs[0].connect).toHaveBeenCalledWith(gains[0]);
    expect(oscs[1].connect).toHaveBeenCalledWith(gains[1]);
    expect(gains[0].connect).toHaveBeenCalledWith(ctx.destination);
    expect(gains[1].connect).toHaveBeenCalledWith(ctx.destination);

    // Envelope + scheduling for the first tone (t=0).
    const now = ctx.currentTime;
    const g0 = gains[0].gain;
    expect(g0.setValueAtTime).toHaveBeenCalledWith(0, now);
    expect(g0.linearRampToValueAtTime).toHaveBeenCalledWith(0.18, now + 0.015);
    expect(g0.exponentialRampToValueAtTime).toHaveBeenCalledWith(0.0001, now + 0.25);
    expect(oscs[0].start).toHaveBeenCalledWith(now);
    expect(oscs[0].stop).toHaveBeenCalledWith(now + 0.27);
    // Second tone is offset by 0.12s.
    expect(oscs[1].start).toHaveBeenCalledWith(now + 0.12);
    expect(oscs[1].stop).toHaveBeenCalledWith(now + 0.12 + 0.27);
  });

  it("resumes the context when it is suspended", async () => {
    const { Ctor, ctx } = makeFakeCtx("suspended");
    setAudioCtor(Ctor);
    const { playAttentionSound } = await fresh();

    playAttentionSound();

    expect(ctx.resume).toHaveBeenCalledTimes(1);
  });

  it("does not resume an already-running context", async () => {
    const { Ctor, ctx } = makeFakeCtx("running");
    setAudioCtor(Ctor);
    const { playAttentionSound } = await fresh();

    playAttentionSound();

    expect(ctx.resume).not.toHaveBeenCalled();
  });

  it("reuses one cached AudioContext across calls", async () => {
    const { Ctor } = makeFakeCtx("running");
    setAudioCtor(Ctor);
    const { playAttentionSound } = await fresh();

    playAttentionSound();
    playAttentionSound();

    expect(Ctor).toHaveBeenCalledTimes(1);
  });

  it("falls back to webkitAudioContext when AudioContext is absent", async () => {
    const { Ctor } = makeFakeCtx("running");
    // AudioContext absent, only the webkit-prefixed constructor exists.
    setAudioCtor(undefined, Ctor);
    const { playAttentionSound } = await fresh();

    playAttentionSound();

    expect(Ctor).toHaveBeenCalledTimes(1);
  });

  it("no-ops without throwing when no AudioContext constructor exists", async () => {
    setAudioCtor(undefined, undefined);
    const { playAttentionSound } = await fresh();

    expect(() => playAttentionSound()).not.toThrow();
  });
});

describe("useAttentionSound", () => {
  beforeEach(() => {
    settingRef.value = undefined;
  });
  afterEach(() => {
    setAudioCtor(undefined, undefined);
  });

  it("plays the ding for a live marker that declares sound: true", async () => {
    const { Ctor, ctx } = makeFakeCtx("running");
    setAudioCtor(Ctor);
    const { useAttentionSound, bus } = await fresh();

    const { unmount } = renderHook(() => useAttentionSound());
    bus.publish("session_marker_changed", {
      session_id: "s1",
      extension_id: "ext",
      marker: { color: "orange", tooltip: "needs you", sound: true },
    });

    expect(ctx.createOscillator).toHaveBeenCalledTimes(2);
    unmount();
  });

  it("ignores markers that do not declare sound", async () => {
    const { Ctor, ctx } = makeFakeCtx("running");
    setAudioCtor(Ctor);
    const { useAttentionSound, bus } = await fresh();

    const { unmount } = renderHook(() => useAttentionSound());
    bus.publish("session_marker_changed", {
      session_id: "s1",
      extension_id: "ext",
      marker: { color: "orange", tooltip: "info" },
    });

    expect(ctx.createOscillator).not.toHaveBeenCalled();
    unmount();
  });

  it("mutes when the named sound_setting is explicitly false", async () => {
    settingRef.value = false;
    const { Ctor, ctx } = makeFakeCtx("running");
    setAudioCtor(Ctor);
    const { useAttentionSound, bus } = await fresh();

    const { unmount } = renderHook(() => useAttentionSound());
    bus.publish("session_marker_changed", {
      session_id: "s1",
      extension_id: "ext",
      marker: {
        color: "orange",
        tooltip: "needs you",
        sound: true,
        sound_setting: "mute_key",
      },
    });

    expect(ctx.createOscillator).not.toHaveBeenCalled();
    unmount();
  });

  it("keeps the declared default in force while the setting cache is cold", async () => {
    // Cache cold → extensionAppSettingValue returns undefined → not === false → plays.
    settingRef.value = undefined;
    const { Ctor, ctx } = makeFakeCtx("running");
    setAudioCtor(Ctor);
    const { useAttentionSound, bus } = await fresh();

    const { unmount } = renderHook(() => useAttentionSound());
    bus.publish("session_marker_changed", {
      session_id: "s1",
      extension_id: "ext",
      marker: {
        color: "orange",
        tooltip: "needs you",
        sound: true,
        sound_setting: "mute_key",
      },
    });

    expect(ctx.createOscillator).toHaveBeenCalledTimes(2);
    unmount();
  });

  it("plays when the named sound_setting is explicitly true", async () => {
    settingRef.value = true;
    const { Ctor, ctx } = makeFakeCtx("running");
    setAudioCtor(Ctor);
    const { useAttentionSound, bus } = await fresh();

    const { unmount } = renderHook(() => useAttentionSound());
    bus.publish("session_marker_changed", {
      session_id: "s1",
      extension_id: "ext",
      marker: {
        color: "orange",
        tooltip: "needs you",
        sound: true,
        sound_setting: "mute_key",
      },
    });

    expect(ctx.createOscillator).toHaveBeenCalledTimes(2);
    unmount();
  });

  it("unsubscribes on unmount (no sound after teardown)", async () => {
    const { Ctor, ctx } = makeFakeCtx("running");
    setAudioCtor(Ctor);
    const { useAttentionSound, bus } = await fresh();

    const { unmount } = renderHook(() => useAttentionSound());
    unmount();
    bus.publish("session_marker_changed", {
      session_id: "s1",
      extension_id: "ext",
      marker: { color: "orange", tooltip: "needs you", sound: true },
    });

    expect(ctx.createOscillator).not.toHaveBeenCalled();
  });
});
