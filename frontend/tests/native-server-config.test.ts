import { afterEach, describe, expect, it } from "vitest";
import {
  clearNativeServerUrl,
  hasNativeServerUrl,
  NATIVE_SERVER_URL_STORAGE_KEY,
  readNativeServerUrl,
  writeNativeServerUrl,
} from "../src/nativeServerConfig";
import { clearStoredToken, setTokens } from "../src/bearerAuth";

describe("native server config", () => {
  afterEach(() => {
    clearNativeServerUrl();
    clearStoredToken();
  });

  it("clears a stale server URL so native boot returns to setup", () => {
    writeNativeServerUrl("http://192.168.1.20:8000");
    expect(hasNativeServerUrl()).toBe(true);
    expect(readNativeServerUrl()).toBe("http://192.168.1.20:8000");

    clearNativeServerUrl();

    expect(localStorage.getItem(NATIVE_SERVER_URL_STORAGE_KEY)).toBeNull();
    expect(hasNativeServerUrl()).toBe(false);
  });

  it("clears bearer tokens when the configured server changes", () => {
    setTokens("access-token", "refresh-token");

    clearStoredToken();

    expect(localStorage.getItem("better_agent_auth_token")).toBeNull();
    expect(localStorage.getItem("better_agent_refresh_token")).toBeNull();
  });
});
