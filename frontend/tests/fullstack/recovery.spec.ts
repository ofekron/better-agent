import type { Page } from "@playwright/test";
import { test, expect } from "./harness/fixtures";
import type { FullStackBackend } from "./harness/backend";
import { createSessionWithPrompt } from "./harness/session";
import { killBackendProcessOnly, spawnBackendAgainstExistingHome } from "./harness/recovery";

// Validates scenario 3 ("Restore") from the repo root CLAUDE.md's "Session
// event ingestion — three scenarios MUST converge": the detached runner and
// the real provider CLI subprocess it spawned keep running even though the
// backend process itself just died. On restart, `recover_all_in_flight`
// (backend/provider.py) + `integrate_recovered_runs` (backend/run_recovery.py)
// must bring the session's render tree back to a consistent state — no
// duplicated or corrupted messages — whether the orphaned provider process
// finished the turn on its own or the backend finalizes it as stopped.

interface SessionSummary {
  id: string;
  is_running?: boolean;
}

async function getSessionsList(page: Page, backend: FullStackBackend): Promise<SessionSummary[]> {
  const res = await page.request.get(`${backend.baseURL}/api/sessions?limit=5`);
  if (!res.ok()) {
    throw new Error(`GET /api/sessions failed: ${res.status()} ${await res.text()}`);
  }
  const body = (await res.json()) as { sessions?: SessionSummary[] };
  return body.sessions ?? [];
}

async function waitForRunning(
  page: Page,
  backend: FullStackBackend,
  sessionId: string,
  timeoutMs: number,
): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const sessions = await getSessionsList(page, backend);
    const match = sessions.find((s) => s.id === sessionId);
    if (match?.is_running) return;
    await new Promise((resolve) => setTimeout(resolve, 300));
  }
  throw new Error(`session ${sessionId} never reached is_running=true within ${timeoutMs}ms`);
}

async function waitForNotRunning(
  page: Page,
  backend: FullStackBackend,
  sessionId: string,
  timeoutMs: number,
): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const sessions = await getSessionsList(page, backend);
    const match = sessions.find((s) => s.id === sessionId);
    if (match && match.is_running === false) return;
    await new Promise((resolve) => setTimeout(resolve, 300));
  }
  throw new Error(`session ${sessionId} never reached is_running=false within ${timeoutMs}ms`);
}

test.describe("backend crash-recovery", () => {
  test("render tree stays consistent (no duplicated messages) after a mid-turn backend crash + restart", async ({
    authedPage: page,
    backend,
  }) => {
    test.setTimeout(180_000);

    await createSessionWithPrompt(
      page,
      "Count from 1 to 40, one number per line, then write a short paragraph " +
        "explaining why counting is a foundational skill. Do not rush, be thorough.",
    );

    const sessionsBeforeCrash = await getSessionsList(page, backend);
    expect(sessionsBeforeCrash.length).toBeGreaterThan(0);
    const sessionId = sessionsBeforeCrash[0].id as string;

    // Wait until the real provider CLI turn is actually in flight before
    // crashing the backend, so the crash genuinely lands mid-turn (the
    // window scenario 3 covers) rather than racing an empty queue.
    await waitForRunning(page, backend, sessionId, 30_000);

    // Kill ONLY the backend/uvicorn process — the real `claude` CLI
    // subprocess it spawned for this turn is left running/orphaned, exactly
    // like an unclean backend crash mid-turn (not a graceful shutdown that
    // would cancel the turn).
    await killBackendProcessOnly(backend);

    // Bring a fresh backend process back up against the SAME isolated home
    // + port, simulating the restart half of "crash + restart".
    const restarted = await spawnBackendAgainstExistingHome(backend);

    try {
      const health = await page.request.get(`${backend.baseURL}/api/auth/needs_setup`);
      expect(health.ok()).toBe(true);

      await page.reload();
      await page.getByTestId("chat-messages").waitFor({ state: "visible", timeout: 20_000 });

      // Give recovery/reconcile a generous window to converge: either the
      // orphaned real provider CLI process finished the turn on its own and
      // recovery integrated it as complete, or the backend detected it was
      // dead/gone and finalized the turn as stopped. Either is an acceptable
      // terminal state per the render-tree convergence invariant — getting
      // stuck "running" forever is not.
      await expect
        .poll(
          async () => {
            const sessions = await getSessionsList(page, backend);
            const match = sessions.find((s) => s.id === sessionId);
            return match?.is_running === true;
          },
          { timeout: 120_000, intervals: [500, 1000, 2000] },
        )
        .toBe(false);

      // Re-fetch the render tree from the NEW backend process directly (not
      // just what happens to still be in the DOM) — this is the same
      // projection REST/WS serve, and is what backend/scripts/
      // test_recovery_render_consistency.py locks at the unit level. The
      // frontend assertions below cross-check that the UI actually reflects
      // this same state.
      const detailRes = await page.request.get(
        `${backend.baseURL}/api/sessions/${encodeURIComponent(sessionId)}?msg_limit=50`,
      );
      expect(detailRes.ok()).toBe(true);
      const tree = await detailRes.json();
      const messages = (tree.messages ?? []) as Array<{ id: string; role: string }>;
      const ids = messages.map((m) => m.id);
      expect(new Set(ids).size, `duplicate message ids in render tree: ${ids.join(",")}`).toBe(
        ids.length,
      );
      expect(messages.filter((m) => m.role === "user")).toHaveLength(1);
      expect(messages.filter((m) => m.role === "assistant")).toHaveLength(1);

      // Reload once more so the UI is guaranteed to be rendering from the
      // now-settled backend state (not a stale in-flight WS projection from
      // before the crash).
      await page.reload();
      await page.getByTestId("chat-messages").waitFor({ state: "visible", timeout: 20_000 });

      const userMessages = page.getByTestId("user-message");
      const assistantMessages = page.getByTestId("assistant-message");

      // No duplicated bubbles: exactly one prompt was sent, so exactly one
      // user bubble and one assistant bubble must render post-recovery —
      // never two copies of the same turn.
      await expect(userMessages).toHaveCount(1);
      await expect(assistantMessages).toHaveCount(1);

      const assistantMessage = assistantMessages.first();
      const text = (await assistantMessage.textContent()) ?? "";
      const hasRealContent = text.trim().length > 0;
      const hasStoppedIndicator = (await assistantMessage.locator(".stopped-indicator").count()) > 0;

      // The bubble must show SOME recognizable terminal signal: real
      // completed content, or a "Stopped"/"Interrupted at ..." indicator —
      // never a permanently blank bubble with nothing to show for it.
      expect(
        hasRealContent || hasStoppedIndicator,
        `assistant bubble has neither content nor a stopped indicator after recovery: ${JSON.stringify(text)}`,
      ).toBe(true);
    } finally {
      await restarted.stop();
    }
  });

  test("render tree stays consistent after a backend crash + restart with no in-flight turn", async ({
    authedPage: page,
    backend,
  }) => {
    test.setTimeout(180_000);

    await createSessionWithPrompt(page, "Reply with exactly the word: done.");

    const sessionsBeforeCrash = await getSessionsList(page, backend);
    expect(sessionsBeforeCrash.length).toBeGreaterThan(0);
    const sessionId = sessionsBeforeCrash[0].id as string;

    // Let the turn fully finish BEFORE crashing — this is the simplest
    // recovery case: no in-flight work at all, nothing for
    // recover_all_in_flight/integrate_recovered_runs to reconcile. The
    // restart should be a pure no-op on the render tree.
    await waitForNotRunning(page, backend, sessionId, 60_000);

    await killBackendProcessOnly(backend);
    const restarted = await spawnBackendAgainstExistingHome(backend);

    try {
      const health = await page.request.get(`${backend.baseURL}/api/auth/needs_setup`);
      expect(health.ok()).toBe(true);

      await page.reload();
      await page.getByTestId("chat-messages").waitFor({ state: "visible", timeout: 20_000 });

      // The session must come back not-running (it already wasn't) and the
      // render tree must be byte-for-byte the same single user+assistant
      // pair — no duplication, no re-run triggered by the restart.
      const sessionsAfterRestart = await getSessionsList(page, backend);
      const matchAfterRestart = sessionsAfterRestart.find((s) => s.id === sessionId);
      expect(matchAfterRestart?.is_running).toBe(false);

      const detailRes = await page.request.get(
        `${backend.baseURL}/api/sessions/${encodeURIComponent(sessionId)}?msg_limit=50`,
      );
      expect(detailRes.ok()).toBe(true);
      const tree = await detailRes.json();
      const messages = (tree.messages ?? []) as Array<{ id: string; role: string }>;
      const ids = messages.map((m) => m.id);
      expect(new Set(ids).size, `duplicate message ids in render tree: ${ids.join(",")}`).toBe(
        ids.length,
      );
      expect(messages.filter((m) => m.role === "user")).toHaveLength(1);
      expect(messages.filter((m) => m.role === "assistant")).toHaveLength(1);

      const userMessages = page.getByTestId("user-message");
      const assistantMessages = page.getByTestId("assistant-message");

      // No duplicated bubbles and no re-run: exactly one prompt was sent
      // and it had already completed before the crash, so the restart must
      // not add a second copy of either bubble.
      await expect(userMessages).toHaveCount(1);
      await expect(assistantMessages).toHaveCount(1);

      const assistantText = (await assistantMessages.first().textContent()) ?? "";
      expect(assistantText.trim().length).toBeGreaterThan(0);
    } finally {
      await restarted.stop();
    }
  });

  test("recovers TWO separate in-flight sessions at once after a single backend crash + restart", async ({
    authedPage: page,
    backend,
  }) => {
    test.setTimeout(240_000);

    // Create two independent real sessions, each with its own prompt that
    // takes a bit of time, capturing each session's id from the URL right
    // after creation (robust regardless of list ordering) rather than
    // relying on GET /api/sessions ordering to disambiguate them.
    await createSessionWithPrompt(
      page,
      "Count from 1 to 40, one number per line, then write a short paragraph " +
        "explaining why counting is a foundational skill. Do not rush, be thorough.",
    );
    const sessionIdA = new URL(page.url()).pathname.replace(/^\/s\//, "");

    await createSessionWithPrompt(
      page,
      "Count from 1 to 40 by twos, one number per line, then write a short " +
        "paragraph explaining why even numbers matter. Do not rush, be thorough.",
    );
    const sessionIdB = new URL(page.url()).pathname.replace(/^\/s\//, "");

    expect(sessionIdA).not.toBe(sessionIdB);

    // Wait until BOTH real provider CLI turns are actually in flight before
    // crashing the backend, so the crash genuinely lands mid-turn for both
    // sessions at once — proving recovery scales per-session rather than
    // only handling a single active session.
    await waitForRunning(page, backend, sessionIdA, 30_000);
    await waitForRunning(page, backend, sessionIdB, 30_000);

    await killBackendProcessOnly(backend);
    const restarted = await spawnBackendAgainstExistingHome(backend);

    try {
      const health = await page.request.get(`${backend.baseURL}/api/auth/needs_setup`);
      expect(health.ok()).toBe(true);

      await page.reload();
      await page.getByTestId("chat-messages").waitFor({ state: "visible", timeout: 20_000 });

      // Give recovery/reconcile a generous window to converge BOTH sessions
      // independently: neither may stay stuck "running" forever.
      for (const sessionId of [sessionIdA, sessionIdB]) {
        await expect
          .poll(
            async () => {
              const sessions = await getSessionsList(page, backend);
              const match = sessions.find((s) => s.id === sessionId);
              return match?.is_running === true;
            },
            { timeout: 120_000, intervals: [500, 1000, 2000] },
          )
          .toBe(false);
      }

      // Re-fetch each session's render tree independently from the NEW
      // backend process and assert per-session consistency: no duplicate
      // message ids, exactly one user + one assistant message each.
      for (const sessionId of [sessionIdA, sessionIdB]) {
        const detailRes = await page.request.get(
          `${backend.baseURL}/api/sessions/${encodeURIComponent(sessionId)}?msg_limit=50`,
        );
        expect(detailRes.ok()).toBe(true);
        const tree = await detailRes.json();
        const messages = (tree.messages ?? []) as Array<{ id: string; role: string }>;
        const ids = messages.map((m) => m.id);
        expect(
          new Set(ids).size,
          `duplicate message ids in render tree for session ${sessionId}: ${ids.join(",")}`,
        ).toBe(ids.length);
        expect(messages.filter((m) => m.role === "user")).toHaveLength(1);
        expect(messages.filter((m) => m.role === "assistant")).toHaveLength(1);
      }
    } finally {
      await restarted.stop();
    }
  });

  test("converges after crashing and restarting the backend TWICE in a row", async ({
    authedPage: page,
    backend,
  }) => {
    test.setTimeout(240_000);

    // Repeated-failure resilience check: a real restart is not necessarily
    // stable on its own (e.g. it could re-adopt an orphan and start a
    // *second* in-flight run that itself gets orphaned by a second crash).
    // Crashing the freshly-restarted backend again, immediately, proves
    // recovery converges even when it never gets a quiet moment to settle.
    await createSessionWithPrompt(
      page,
      "Count from 1 to 40, one number per line, then write a short paragraph " +
        "explaining why counting is a foundational skill. Do not rush, be thorough.",
    );

    const sessionsBeforeCrash = await getSessionsList(page, backend);
    expect(sessionsBeforeCrash.length).toBeGreaterThan(0);
    const sessionId = sessionsBeforeCrash[0].id as string;

    await waitForRunning(page, backend, sessionId, 30_000);

    // First crash + restart.
    await killBackendProcessOnly(backend);
    const restarted1 = await spawnBackendAgainstExistingHome(backend);

    // `spawnBackendAgainstExistingHome` returns a `RestartedBackend`
    // ({baseURL, port, pid, stop}), not a `FullStackBackend` — it's missing
    // `username`/`password`/`homeDir`, which `killBackendProcessOnly` and
    // `spawnBackendAgainstExistingHome` need (the latter to locate the
    // headless-auth credential files under `<homeDir>/_auth/`). Those three
    // fields don't change across a restart (same isolated home), so the
    // restarted process is fed back in by reusing them from the original
    // `backend` and overlaying the new `baseURL`/`port`/`stop` — no change
    // to backend.ts or recovery.ts was needed.
    const backendAfterFirstRestart: FullStackBackend = {
      ...backend,
      baseURL: restarted1.baseURL,
      port: restarted1.port,
      stop: restarted1.stop,
    };

    let restarted2: Awaited<ReturnType<typeof spawnBackendAgainstExistingHome>> | undefined;
    try {
      // Second crash + restart, immediately, on the backend that just came
      // back up — no quiet/settled window in between.
      await killBackendProcessOnly(backendAfterFirstRestart);
      restarted2 = await spawnBackendAgainstExistingHome(backendAfterFirstRestart);

      const health = await page.request.get(`${restarted2.baseURL}/api/auth/needs_setup`);
      expect(health.ok()).toBe(true);

      await page.reload();
      await page.getByTestId("chat-messages").waitFor({ state: "visible", timeout: 20_000 });

      await expect
        .poll(
          async () => {
            const sessions = await getSessionsList(page, backend);
            const match = sessions.find((s) => s.id === sessionId);
            return match?.is_running === true;
          },
          { timeout: 120_000, intervals: [500, 1000, 2000] },
        )
        .toBe(false);

      const detailRes = await page.request.get(
        `${backend.baseURL}/api/sessions/${encodeURIComponent(sessionId)}?msg_limit=50`,
      );
      expect(detailRes.ok()).toBe(true);
      const tree = await detailRes.json();
      const messages = (tree.messages ?? []) as Array<{ id: string; role: string }>;
      const ids = messages.map((m) => m.id);
      expect(new Set(ids).size, `duplicate message ids in render tree: ${ids.join(",")}`).toBe(
        ids.length,
      );
      expect(messages.filter((m) => m.role === "user")).toHaveLength(1);
      expect(messages.filter((m) => m.role === "assistant")).toHaveLength(1);

      await page.reload();
      await page.getByTestId("chat-messages").waitFor({ state: "visible", timeout: 20_000 });

      const userMessages = page.getByTestId("user-message");
      const assistantMessages = page.getByTestId("assistant-message");

      await expect(userMessages).toHaveCount(1);
      await expect(assistantMessages).toHaveCount(1);

      const assistantMessage = assistantMessages.first();
      const text = (await assistantMessage.textContent()) ?? "";
      const hasRealContent = text.trim().length > 0;
      const hasStoppedIndicator = (await assistantMessage.locator(".stopped-indicator").count()) > 0;

      expect(
        hasRealContent || hasStoppedIndicator,
        `assistant bubble has neither content nor a stopped indicator after double recovery: ${JSON.stringify(text)}`,
      ).toBe(true);
    } finally {
      await restarted2?.stop();
    }
  });

  test("crash + restart with ZERO sessions ever created recovers cleanly", async ({
    authedPage: page,
    backend,
  }) => {
    test.setTimeout(60_000);

    // The cleanest possible recovery case: no session was ever created, so
    // there is nothing at all for recover_all_in_flight/
    // integrate_recovered_runs to reconcile. This is the baseline the other
    // tests in this file build on top of — a restart must not materialize
    // any phantom/corrupted session out of thin air.
    const sessionsBeforeCrash = await getSessionsList(page, backend);
    expect(sessionsBeforeCrash).toHaveLength(0);

    await killBackendProcessOnly(backend);
    const restarted = await spawnBackendAgainstExistingHome(backend);

    try {
      const health = await page.request.get(`${backend.baseURL}/api/auth/needs_setup`);
      expect(health.ok()).toBe(true);

      await page.reload();
      await page.locator(".session-new-button").waitFor({ state: "visible", timeout: 20_000 });

      const sessionsAfterRestart = await getSessionsList(page, backend);
      expect(sessionsAfterRestart).toHaveLength(0);
    } finally {
      await restarted.stop();
    }
  });

  test("session accepts and completes a brand new real turn immediately after crash + restart", async ({
    authedPage: page,
    backend,
  }) => {
    test.setTimeout(180_000);

    // Beyond render-tree consistency: a recovered session must not just
    // look right, it must still be a fully USABLE session — able to accept
    // and complete a brand new turn, not left read-only/wedged.
    await createSessionWithPrompt(
      page,
      "Count from 1 to 40, one number per line, then write a short paragraph " +
        "explaining why counting is a foundational skill. Do not rush, be thorough.",
    );

    const sessionsBeforeCrash = await getSessionsList(page, backend);
    expect(sessionsBeforeCrash.length).toBeGreaterThan(0);
    const sessionId = sessionsBeforeCrash[0].id as string;

    await waitForRunning(page, backend, sessionId, 30_000);

    await killBackendProcessOnly(backend);
    const restarted = await spawnBackendAgainstExistingHome(backend);

    try {
      const health = await page.request.get(`${backend.baseURL}/api/auth/needs_setup`);
      expect(health.ok()).toBe(true);

      await page.reload();
      await page.getByTestId("chat-messages").waitFor({ state: "visible", timeout: 20_000 });

      // Simplified version of the convergence check reused from the first
      // test in this file: the old (crashed-mid-turn) turn must settle to a
      // terminal, not-running state before this test drives a new one.
      await expect
        .poll(
          async () => {
            const sessions = await getSessionsList(page, backend);
            const match = sessions.find((s) => s.id === sessionId);
            return match?.is_running === true;
          },
          { timeout: 120_000, intervals: [500, 1000, 2000] },
        )
        .toBe(false);

      const userMessages = page.getByTestId("user-message");
      const assistantMessages = page.getByTestId("assistant-message");
      await expect(userMessages).toHaveCount(1);
      await expect(assistantMessages).toHaveCount(1);

      // Now prove the session is actually USABLE post-recovery: send a
      // SECOND real new prompt through the real UI/backend/provider CLI
      // path (same input the user would use), not just a passive read of
      // already-settled state.
      const secondPrompt = "Reply with exactly the single word: RECOVERED. No punctuation, no other words.";
      await page.getByTestId("input-textarea").fill(secondPrompt);
      await page.getByTestId("send-btn").click();

      await expect(userMessages).toHaveCount(2, { timeout: 15_000 });
      await expect(assistantMessages).toHaveCount(2, { timeout: 30_000 });

      // The new turn must get a REAL new reply — content actually derived
      // from the new prompt — proving the session was left fully usable,
      // not stuck in some broken/read-only state after recovery.
      await expect(assistantMessages.nth(1)).toContainText("RECOVERED", { timeout: 120_000 });
    } finally {
      await restarted.stop();
    }
  });
});
