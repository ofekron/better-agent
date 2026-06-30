import { beforeEach, describe, expect, it, vi } from "vitest";

const NATIVE_KEY = "better_agent_auth_token";

function setNative(value: boolean) {
  vi.doMock("@capacitor/core", () => ({
    Capacitor: { isNativePlatform: () => value },
  }));
}

async function importUtil() {
  vi.resetModules();
  const mod = await import("../src/utils/rawFileUrl");
  return mod.rawFileUrl;
}

describe("rawFileUrl", () => {
  beforeEach(() => localStorage.clear());

  it("appends ?token= on native so the media request authenticates", async () => {
    localStorage.setItem(NATIVE_KEY, "bearer-123");
    setNative(true);
    const rawFileUrl = await importUtil();
    const url = rawFileUrl("https://host", "dir/video.mp4", "primary", 7);
    expect(url).toContain("/api/file/raw?path=dir%2Fvideo.mp4&node_id=primary&_v=7");
    expect(url).toContain("&token=bearer-123");
  });

  it("omits the token on web (cookie path; no URL leak)", async () => {
    localStorage.setItem(NATIVE_KEY, "bearer-123");
    setNative(false);
    const rawFileUrl = await importUtil();
    const url = rawFileUrl("https://host", "a/b.mp4", "primary");
    expect(url).not.toContain("token=");
  });

  it("omits the token when no token is stored, even on native", async () => {
    setNative(true);
    const rawFileUrl = await importUtil();
    const url = rawFileUrl("https://host", "a.mp4", "primary");
    expect(url).not.toContain("token=");
  });
});
