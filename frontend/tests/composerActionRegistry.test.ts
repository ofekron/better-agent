import { describe, expect, it, vi } from "vitest";
import {
  buildComposerActionRegistry,
  composerActionsForSurface,
} from "../src/utils/composerActionRegistry";

const labels = { send: "Send", queue: "Queue", steer: "Steer", interrupt: "Interrupt" };

describe("composer action registry", () => {
  it("keeps steer and queue above the mobile composer while interrupt stays in overflow", () => {
    const actions = buildComposerActionRegistry({
      running: true,
      steerable: true,
      send: vi.fn(),
      steer: vi.fn(),
      interrupt: vi.fn(),
      labels,
    });
    expect(composerActionsForSurface(actions, "mobileTop").map((item) => item.id)).toEqual([
      "steer",
      "queue",
    ]);
    expect(composerActionsForSurface(actions, "mobileOverflow").map((item) => item.id)).toEqual([
      "interrupt",
    ]);
  });

  it("falls back from steer to queue when no active steerable turn exists", () => {
    const actions = buildComposerActionRegistry({
      running: false,
      steerable: true,
      send: vi.fn(),
      steer: vi.fn(),
      labels,
    });
    expect(composerActionsForSurface(actions, "primary").map((item) => item.id)).toEqual(["send"]);
  });
});
