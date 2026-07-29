import { describe, expect, it } from "vitest";
import { renderApp } from "./harness";

const nativeFetch = globalThis.fetch;
const nativeWebSocket = globalThis.WebSocket;

describe("harness cleanup ownership", () => {
  it("may exit without manually unmounting", async () => {
    await renderApp();
  });

  it("restores global transports after the prior test", () => {
    expect(globalThis.fetch).toBe(nativeFetch);
    expect(globalThis.WebSocket).toBe(nativeWebSocket);
  });
});
