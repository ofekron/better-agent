import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { renderApp } from "./harness";
import { makeSession } from "./fixtures";
import { OFFLINE_TAB_ID } from "../src/lib/offlineFlushLock";

/**
 * The offline backlog is shared across every tab of the origin, so a tab
 * booting into a backlog that ANOTHER tab is already delivering must not put
 * the same prompt on the wire again. Production hit this as one prompt
 * arriving at the backend nine times (settings page + extension panels +
 * four session tabs all flushing the same records), with the optimistic
 * "Sending..." bubble rendered next to the queued banner for the same text.
 *
 * Re-dispatch is driven by events, never by elapsed time: our own socket
 * dropping, or the owning tab ceasing to exist.
 */

const STORAGE_KEY = "better_agent_offline_queue";
const SID = "sess-1";

function seedBacklog(owner?: string): void {
  localStorage.setItem(
    STORAGE_KEY,
    JSON.stringify([
      {
        sessionId: SID,
        clientId: "pending-1",
        prompt: "queued while another tab was sending",
        model: "sonnet",
        cwd: "/tmp/project",
        sendMode: "queue",
        ...(owner ? { dispatchedBy: owner } : {}),
      },
    ]),
  );
}

function sendsFor(frames: readonly { type: string; [k: string]: unknown }[]): unknown[] {
  return frames.filter((f) => f.type === "send_message" && f.client_id === "pending-1");
}

async function renderWithBacklog() {
  const h = await renderApp({ seed: { sessions: [makeSession({ id: SID })] } });
  await h.selectSession(SID);
  await h.flush();
  return h;
}

beforeEach(() => {
  localStorage.clear();
  // This suite's own clear() wipes the global Chat Surface Contract v2
  // kill-switch pin (tests/setup.ts) since this file's beforeEach runs
  // after it — re-pin so selectSession stays on the legacy hydration/WS
  // path this suite's outbound-frame assertions are written against.
  localStorage.setItem("ba.surface_v2", "0");
});
afterEach(() => {
  localStorage.clear();
  Reflect.deleteProperty(navigator, "locks");
});

describe("offline backlog flush — cross-tab redispatch", () => {
  it("does not re-send an action another tab already claimed", async () => {
    seedBacklog("other-tab");
    const h = await renderWithBacklog();

    expect(sendsFor(h.outbound)).toHaveLength(0);
  });

  it("sends an unclaimed backlog action exactly once", async () => {
    seedBacklog();
    const h = await renderWithBacklog();
    // Further effect passes must not produce a second copy.
    await h.flush();

    expect(sendsFor(h.outbound)).toHaveLength(1);
  });

  it("keeps another tab's claim across OUR reconnect", async () => {
    seedBacklog("other-tab");
    const h = await renderWithBacklog();

    h.dropConnection();
    await h.flush();
    h.reopenConnection();
    await h.flush();

    // Our socket died; the owning tab's did not.
    expect(sendsFor(h.outbound)).toHaveLength(0);
  });

  it("re-delivers OUR OWN claim across a reconnect, exactly once per delivery", async () => {
    seedBacklog(OFFLINE_TAB_ID);
    // Our own claim with no live socket is undelivered by definition, so the
    // first flush reclaims and sends it.
    const h = await renderWithBacklog();
    expect(sendsFor(h.outbound)).toHaveLength(1);

    h.dropConnection();
    await h.flush();
    h.reopenConnection();
    await h.flush();

    // The drop re-armed it; reconnect redelivers once, not repeatedly.
    expect(sendsFor(h.outbound)).toHaveLength(2);
  });

  it("reclaims an action stranded by a tab that no longer exists", async () => {
    // Web Locks reports which tabs still hold a presence lock. "dead-tab" is
    // absent from that set, so nothing is waiting on its send.
    Object.defineProperty(navigator, "locks", {
      configurable: true,
      value: {
        request: (_name: string, ...rest: unknown[]) => {
          const cb = rest[rest.length - 1] as () => unknown;
          return Promise.resolve(cb());
        },
        query: async () => ({
          held: [{ name: `better-agent-tab:${OFFLINE_TAB_ID}` }],
          pending: [],
        }),
      },
    });
    seedBacklog("dead-tab");
    const h = await renderWithBacklog();
    await h.flush();

    expect(sendsFor(h.outbound)).toHaveLength(1);
  });
});
