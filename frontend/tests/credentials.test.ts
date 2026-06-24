import { describe, it, expect } from "vitest";
import { renderApp } from "./harness";
import { makeSession } from "./fixtures";
import type { CredentialConsent } from "../src/types";

function makeConsent(overrides: Partial<CredentialConsent> = {}): CredentialConsent {
  return {
    consent_id: "c1",
    app_session_id: "sess-1",
    provider_id: "prov-1",
    label: "GitHub API",
    sink: {
      sink_kind: "http",
      computed_host: "api.github.com",
      computed_target: "GET https://api.github.com/user",
      egress: true,
      risk: "low",
      risk_reasons: [],
      label_mismatch: false,
    },
    status: "pending",
    secret_names: ["secret"],
    created_at: new Date().toISOString(),
    expires_at: new Date(Date.now() + 86400_000).toISOString(),
    use_count: 0,
    ...overrides,
  };
}

describe("credential consent cards", () => {
  it("renders one card per pending consent, showing the REAL computed sink", async () => {
    const session = makeSession({ id: "sess-1" });
    const h = await renderApp({
      seed: {
        sessions: [session],
        credentials: [
          makeConsent({ consent_id: "c1" }),
          makeConsent({
            consent_id: "c2",
            label: "Stripe",
            sink: {
              sink_kind: "http",
              computed_host: "api.stripe.com",
              computed_target: "POST https://api.stripe.com/charges",
              egress: true,
              risk: "high",
              risk_reasons: ["non-idempotent method POST"],
              label_mismatch: false,
            },
          }),
        ],
      },
    });
    await h.selectSession(session.id);
    await h.flush();

    const cards = h.toJSON().chat.credentials;
    expect(cards.map((c) => c.consentId).sort()).toEqual(["c1", "c2"]);
    const c1 = cards.find((c) => c.consentId === "c1")!;
    expect(c1.sinkText).toContain("api.github.com");
    h.unmount();
  });

  it("flags a label/host mismatch loudly (anti-deception)", async () => {
    const session = makeSession({ id: "sess-1" });
    const h = await renderApp({
      seed: {
        sessions: [session],
        credentials: [
          makeConsent({
            consent_id: "c-mismatch",
            label: "github.com login",
            sink: {
              sink_kind: "http",
              computed_host: "evil.com",
              computed_target: "GET https://evil.com/steal",
              egress: true,
              risk: "low",
              risk_reasons: [],
              label_mismatch: true,
            },
          }),
        ],
      },
    });
    await h.selectSession(session.id);
    await h.flush();

    const card = h.toJSON().chat.credentials[0];
    expect(card.mismatch).toBe(true);
    expect(card.egress).toBe(true);
    expect(card.sinkText).toContain("evil.com");
    h.unmount();
  });

  it("shows the high-risk tier badge for state-changing ops", async () => {
    const session = makeSession({ id: "sess-1" });
    const h = await renderApp({
      seed: {
        sessions: [session],
        credentials: [
          makeConsent({
            consent_id: "c-hi",
            sink: {
              sink_kind: "http",
              computed_host: "api.github.com",
              computed_target: "POST https://api.github.com/issues",
              egress: true,
              risk: "high",
              risk_reasons: ["non-idempotent method POST"],
              label_mismatch: false,
            },
          }),
        ],
      },
    });
    await h.selectSession(session.id);
    await h.flush();

    expect(h.toJSON().chat.credentials[0].risk).toContain("high");
    h.unmount();
  });

  it("Approve posts the secret in the body and removes the card", async () => {
    const session = makeSession({ id: "sess-1" });
    const h = await renderApp({
      seed: { sessions: [session], credentials: [makeConsent({ consent_id: "c-ap" })] },
    });
    await h.selectSession(session.id);
    await h.flush();
    expect(h.toJSON().chat.credentials).toHaveLength(1);

    await h.approveCredential("c-ap", "sk-THE-SECRET");

    const call = h.backend.calls.find(
      (c) => c.method === "POST" && c.path === "/api/credentials/c-ap/approve",
    );
    expect(call).toBeDefined();
    expect((call!.body as { secrets?: Record<string, string> }).secrets).toEqual({
      secret: "sk-THE-SECRET",
    });
    expect(h.toJSON().chat.credentials).toHaveLength(0);
    h.unmount();
  });

  it("approves stored password-manager secrets without rendering a password input", async () => {
    const session = makeSession({ id: "sess-1" });
    const h = await renderApp({
      seed: {
        sessions: [session],
        credentials: [
          makeConsent({
            consent_id: "c-stored",
            secret_names: ["sftp.host", "sftp.user", "sftp.pass"],
            secret_sources: {
              "sftp.host": {
                kind: "password_manager",
                service: "ofekdev",
                account: "sftp.host",
              },
              "sftp.user": {
                kind: "password_manager",
                service: "ofekdev",
                account: "sftp.user",
              },
              "sftp.pass": {
                kind: "password_manager",
                service: "ofekdev",
                account: "sftp.pass",
              },
            },
          }),
        ],
      },
    });
    await h.selectSession(session.id);
    await h.flush();

    const card = h.toJSON().chat.credentials[0];
    expect(card.text).toContain("ofekdev/sftp.pass");
    expect(card.text).not.toContain("Paste secret");

    await h.approveCredential("c-stored");

    const call = h.backend.calls.find(
      (c) => c.method === "POST" && c.path === "/api/credentials/c-stored/approve",
    );
    expect(call).toBeDefined();
    expect(call!.body).toEqual({});
    expect(h.toJSON().chat.credentials).toHaveLength(0);
    h.unmount();
  });

  it("Deny posts /deny and removes the card", async () => {
    const session = makeSession({ id: "sess-1" });
    const h = await renderApp({
      seed: { sessions: [session], credentials: [makeConsent({ consent_id: "c-dn" })] },
    });
    await h.selectSession(session.id);
    await h.flush();

    await h.denyCredential("c-dn");
    expect(
      h.backend.calls.find(
        (c) => c.method === "POST" && c.path === "/api/credentials/c-dn/deny",
      ),
    ).toBeDefined();
    expect(h.toJSON().chat.credentials).toHaveLength(0);
    h.unmount();
  });

  it("credential_consent_changed WS ping refetches the pending list", async () => {
    const session = makeSession({ id: "sess-1" });
    const h = await renderApp({
      seed: { sessions: [session], credentials: [] },
    });
    await h.selectSession(session.id);
    await h.flush();
    expect(h.toJSON().chat.credentials).toHaveLength(0);

    // Backend gains a pending consent, then pings; the card must appear
    // without a manual refresh (state-ownership: pull on push).
    h.backend.state.credentials.push(makeConsent({ consent_id: "c-push" }));
    await h.typeAndSend("trigger");
    h.emit({ type: "turn_start", data: { session_id: session.id } });
    h.emit({
      type: "credential_consent_changed",
      data: { app_session_id: "sess-1" },
    });
    await h.flush();

    expect(
      h.toJSON().chat.credentials.map((c) => c.consentId),
    ).toContain("c-push");
    h.unmount();
  });

  it("never exposes the secret value back in the rendered card", async () => {
    const session = makeSession({ id: "sess-1" });
    const h = await renderApp({
      seed: { sessions: [session], credentials: [makeConsent({ consent_id: "c-x" })] },
    });
    await h.selectSession(session.id);
    await h.flush();
    // The consent view from the backend carries no secret; assert the
    // rendered card text never contains a secret-looking value.
    const card = h.toJSON().chat.credentials[0];
    expect(card.text).not.toContain("sk-");
    h.unmount();
  });
});
