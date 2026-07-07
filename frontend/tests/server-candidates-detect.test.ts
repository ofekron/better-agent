import { describe, expect, it } from "vitest";
import {
  collectCandidates,
  tailscaleHttpsCandidateFromStatus,
} from "../scripts/detect-ips.mjs";

describe("detect-ips", () => {
  it("prefers Tailscale HTTPS before IP candidates", () => {
    const candidates = collectCandidates({
      tailscaleHttps: "https://mac.tailnet.ts.net",
      ifaces: {
        en0: [{ family: "IPv4", internal: false, address: "192.168.1.20" }],
        tailscale0: [{ family: "IPv4", internal: false, address: "100.101.102.103" }],
      },
    });

    expect(candidates).toEqual([
      "https://mac.tailnet.ts.net",
      "http://100.101.102.103:18765",
      "http://192.168.1.20:18765",
    ]);
  });

  it("falls back to local IP candidates without Tailscale HTTPS", () => {
    const candidates = collectCandidates({
      tailscaleHttps: "",
      ifaces: {
        en0: [{ family: "IPv4", internal: false, address: "192.168.1.20" }],
      },
    });

    expect(candidates).toEqual(["http://192.168.1.20:18765"]);
  });

  it("only accepts active ts.net DNS names from Tailscale status", () => {
    expect(tailscaleHttpsCandidateFromStatus({
      BackendState: "Running",
      Self: { DNSName: "Mac.Tailnet.ts.net.", Online: true },
    })).toBe("https://mac.tailnet.ts.net");

    expect(tailscaleHttpsCandidateFromStatus({
      BackendState: "Stopped",
      Self: { DNSName: "mac.tailnet.ts.net.", Online: true },
    })).toBe("");
    expect(tailscaleHttpsCandidateFromStatus({
      BackendState: "Running",
      Self: { DNSName: "mac.local.", Online: true },
    })).toBe("");
    expect(tailscaleHttpsCandidateFromStatus({
      BackendState: "Running",
      Self: { DNSName: "mac.tailnet.ts.net:443", Online: true },
    })).toBe("");
  });
});
