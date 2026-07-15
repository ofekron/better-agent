import { waitFor } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import { renderApp } from "./harness";
import { makeSession } from "./fixtures";
import type { CredentialConsent } from "../src/types";
import { eventBus } from "../src/lib/eventBus";

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

async function waitForCredentialIds(
  h: Awaited<ReturnType<typeof renderApp>>,
  ids: string[],
): Promise<void> {
  await waitFor(
    () => {
      expect(h.toJSON().chat.credentials.map((card) => card.consentId).sort()).toEqual(
        [...ids].sort(),
      );
    },
    { timeout: 5_000 },
  );
}

describe("credential consent cards", () => {
  it("does not request credentials while the broker id is absent", async () => {
    const session = makeSession({ id: "sess-1" });
    const h = await renderApp({
      seed: { sessions: [session] },
      builtinExtensionIds: {},
    });
    await h.selectSession(session.id);
    h.emit({
      type: "credential_consent_changed",
      data: { app_session_id: session.id },
    });
    await h.flush();

    expect(h.backend.calls.filter((call) => call.path.includes("/credentials"))).toEqual([]);
    h.unmount();
  });

  it("hydrates a late broker id and requests one valid encoded extension URL", async () => {
    const session = makeSession({ id: "sess-1" });
    const h = await renderApp({
      seed: { sessions: [session], credentials: [makeConsent()] },
      builtinExtensionIds: {},
    });
    await h.selectSession(session.id);
    expect(h.backend.calls.filter((call) => call.path.includes("/credentials"))).toEqual([]);

    // The backend is the id map's source of truth: `extensions_changed`
    // makes the app re-pull /api/extensions/builtin-ids, so hydrate the
    // late id there rather than poking the in-memory map (a manual poke
    // would be clobbered by that reload).
    h.backend.state.builtinExtensionIds = {
      credentialBroker: "ofek-dev.credential-broker",
    };
    eventBus.publish("extensions_changed", {});
    await h.flush();
    await h.flush();

    const calls = h.backend.calls.filter((call) => call.path.includes("/credentials/pending"));
    expect(calls).toHaveLength(1);
    expect(calls[0].path).toBe(
      "/api/extensions/ofek-dev.credential-broker/backend/credentials/pending",
    );
    await waitForCredentialIds(h, ["c1"]);
    h.unmount();
  });

  it("ignores a stale pending response after switching sessions", async () => {
    const first = makeSession({ id: "sess-1", name: "First" });
    const second = makeSession({ id: "sess-2", name: "Second" });
    const h = await renderApp({ seed: { sessions: [first, second], credentials: [makeConsent()] } });
    const path = "/api/extensions/ofek-dev.credential-broker/backend/credentials/pending";
    const release = h.backend.holdNext("GET", path);

    await h.selectSession(first.id);
    await h.selectSession(second.id);
    release();
    await h.flush();

    expect(h.toJSON().chat.credentials).toEqual([]);
    h.unmount();
  });

  it("does not request a known broker id when backend-owned enabled state is false", async () => {
    const session = makeSession({ id: "sess-1" });
    const h = await renderApp({
      seed: { sessions: [session], credentialBrokerEnabled: false },
    });
    await h.selectSession(session.id);
    await h.flush();

    expect(h.backend.calls.filter((call) => call.path.includes("/credentials"))).toEqual([]);
    expect(h.toJSON().chat.credentials).toEqual([]);
    h.unmount();
  });

  it("clears credentials and ignores an in-flight response when the broker is disabled", async () => {
    const session = makeSession({ id: "sess-1" });
    const path = "/api/extensions/ofek-dev.credential-broker/backend/credentials/pending";
    const h = await renderApp({ seed: { sessions: [session], credentials: [makeConsent()] } });
    const release = h.backend.holdNext("GET", path);
    await h.selectSession(session.id);

    h.backend.state.credentialBrokerEnabled = false;
    eventBus.publish("extensions_changed", {});
    await h.flush();
    release();
    await h.flush();

    expect(h.toJSON().chat.credentials).toEqual([]);
    h.unmount();
  });

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

    await waitForCredentialIds(h, ["c1", "c2"]);
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

    await waitForCredentialIds(h, ["c-mismatch"]);
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

    await waitForCredentialIds(h, ["c-hi"]);
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
    await waitForCredentialIds(h, ["c-ap"]);

    await h.approveCredential("c-ap", "sk-THE-SECRET");

    const call = h.backend.calls.find(
      (c) => c.method === "POST" && c.path === "/api/extensions/ofek-dev.credential-broker/backend/credentials/c-ap/approve",
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

    await waitForCredentialIds(h, ["c-stored"]);
    const card = h.toJSON().chat.credentials[0];
    expect(card.text).toContain("ofekdev/sftp.pass");
    expect(card.text).not.toContain("Paste secret");

    await h.approveCredential("c-stored");

    const call = h.backend.calls.find(
      (c) => c.method === "POST" && c.path === "/api/extensions/ofek-dev.credential-broker/backend/credentials/c-stored/approve",
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

    await waitForCredentialIds(h, ["c-dn"]);
    await h.denyCredential("c-dn");
    expect(
      h.backend.calls.find(
        (c) => c.method === "POST" && c.path === "/api/extensions/ofek-dev.credential-broker/backend/credentials/c-dn/deny",
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

    await waitForCredentialIds(h, ["c-push"]);
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
    await waitForCredentialIds(h, ["c-x"]);
    const card = h.toJSON().chat.credentials[0];
    expect(card.text).not.toContain("sk-");
    h.unmount();
  });
});
