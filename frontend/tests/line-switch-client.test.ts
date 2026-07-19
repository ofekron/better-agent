import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  clearLineSwitchConnection,
  parseLineSwitchAccessUrl,
  readLineSwitchConnection,
  targetServerUrl,
  writeLineSwitchConnection,
} from "../src/lineSwitchClient";
import { applyNativeServerConfigUrl, nativeConfigUrlForLineSwitch } from "../src/mobileServerHandoff";

vi.mock("@capacitor/core", () => ({ Capacitor: { isNativePlatform: () => false } }));

beforeEach(() => localStorage.clear());

describe("independent line switch pairing", () => {
  it("persists a fragment credential without putting it in the base URL", () => {
    const token = "x".repeat(43);
    const connection = parseLineSwitchAccessUrl(`http://192.168.1.20:18768/#${token}`);
    expect(connection).toEqual({ baseUrl: "http://192.168.1.20:18768", token });
    writeLineSwitchConnection(connection);
    expect(readLineSwitchConnection()).toEqual(connection);
    clearLineSwitchConnection();
    expect(readLineSwitchConnection()).toBeNull();
  });

  it("rejects missing credentials and credential-bearing authorities", () => {
    expect(() => parseLineSwitchAccessUrl("http://host:18768/")).toThrow();
    expect(() => parseLineSwitchAccessUrl(`http://user:pass@host:18768/#${"x".repeat(43)}`)).toThrow();
  });

  it("rewrites loopback line targets to the controller machine", () => {
    const connection = { baseUrl: "http://100.64.0.8:18768", token: "x".repeat(43) };
    expect(targetServerUrl({
      active_line: "dev",
      lines: { dev: "/dev", qa: "/qa" },
      line_targets: { qa: { backend_port: 18767, backend_url: "http://127.0.0.1:18767" } },
      incompatible: {},
      switchable: true,
    }, "qa", connection)).toBe("http://100.64.0.8:18767");
  });

  it("pairs the native app through the existing configure deep link", () => {
    const access = `http://100.64.0.8:18768/#${"x".repeat(43)}`;
    expect(applyNativeServerConfigUrl(nativeConfigUrlForLineSwitch(access))).toBe(true);
    expect(readLineSwitchConnection()?.baseUrl).toBe("http://100.64.0.8:18768");
  });
});
