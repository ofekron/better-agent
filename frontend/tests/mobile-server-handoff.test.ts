import { afterEach, describe, expect, it } from "vitest";
import {
  applyNativeServerConfigUrl,
  mobileInstallUrl,
  nativeConfigUrlForServer,
  serverUrlFromSearch,
} from "../src/mobileServerHandoff";
import { clearNativeServerUrl, readNativeServerUrl } from "../src/nativeServerConfig";
import { clearStoredToken, setTokens } from "../src/bearerAuth";

describe("mobile server handoff", () => {
  afterEach(() => {
    clearNativeServerUrl();
    clearStoredToken();
  });

  it("bundles the backend server address into install URLs", () => {
    expect(mobileInstallUrl("http://192.168.1.20:18765", "android")).toBe(
      "http://192.168.1.20:18765/?download=android&server=http%3A%2F%2F192.168.1.20%3A18765",
    );
  });

  it("builds a native app configure URL from a server address", () => {
    expect(nativeConfigUrlForServer("192.168.1.20")).toBe(
      "betteragent://configure?server=http%3A%2F%2F192.168.1.20%3A18765",
    );
  });

  it("extracts and validates the server address from the handoff query", () => {
    expect(serverUrlFromSearch("?download=android&server=http%3A%2F%2F192.168.1.20%3A18765")).toBe(
      "http://192.168.1.20:18765",
    );
    expect(serverUrlFromSearch("?download=android&server=%20")).toBeNull();
  });

  it("configures native app storage and clears old tokens from a deep link", () => {
    setTokens("old-access", "old-refresh");

    expect(applyNativeServerConfigUrl("betteragent://configure?server=http%3A%2F%2F192.168.1.20%3A18765")).toBe(true);

    expect(readNativeServerUrl()).toBe("http://192.168.1.20:18765");
    expect(localStorage.getItem("better_agent_auth_token")).toBeNull();
    expect(localStorage.getItem("better_agent_refresh_token")).toBeNull();
  });

  it("ignores repeated deep links for the already-configured server", () => {
    const url = "betteragent://configure?server=http%3A%2F%2F192.168.1.20%3A18765";

    expect(applyNativeServerConfigUrl(url)).toBe(true);
    expect(applyNativeServerConfigUrl(url)).toBe(false);
  });

  it("rejects unrelated URLs", () => {
    expect(applyNativeServerConfigUrl("betteragent://shared")).toBe(false);
    expect(applyNativeServerConfigUrl("https://example.test/?server=http://192.168.1.20:18765")).toBe(false);
  });
});
