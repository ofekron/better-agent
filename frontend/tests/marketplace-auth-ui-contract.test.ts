import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

const repoRoot = resolve(import.meta.dirname, "../..");

describe("marketplace settings authentication", () => {
  it("uses the nonce-bound core bridge and never stores marketplace tokens in the iframe", () => {
    const html = readFileSync(resolve(repoRoot, "extensions/marketplace/ui/index.html"), "utf8");

    expect(html).toContain('action: "marketplace-auth-start"');
    expect(html).toContain('action: "marketplace-request"');
    expect(html).toContain("pendingRequests.get(event.data.requestId)");
    expect(html).toContain('event.data.action === "marketplace-auth-init"');
    expect(html).toContain("refreshEl.disabled = false");
    expect(html).toContain('event.data.nonce !== bridgeNonce');
    expect(html).toContain("/backend/auth/status");
    expect(html).toContain("/backend/auth/logout");
    expect(html).not.toContain("better-agent.marketplace.accessToken");
    expect(html).not.toContain("Marketplace access token");
    expect(html).not.toContain("window.localStorage");
    expect(html).not.toContain("fetch(");
    expect(html).toContain('requestAction("marketplace-install"');
    expect(html).not.toContain('requestJson("/api/extensions/install"');
  });

  it("declares the dedicated marketplace authentication permission", () => {
    const manifest = JSON.parse(
      readFileSync(resolve(repoRoot, "extensions/marketplace/better-agent-extension.json"), "utf8"),
    ) as { permissions?: Record<string, unknown> };

    expect(manifest.permissions?.marketplace_auth).toBe(true);
  });
});
