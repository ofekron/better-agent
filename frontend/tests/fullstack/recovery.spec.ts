import type { ChildProcess } from "node:child_process";
import { spawn } from "node:child_process";
import path from "node:path";
import type { Page } from "@playwright/test";
import { test, expect } from "./harness/fixtures";
import type { FullStackBackend } from "./harness/backend";
import { createSessionWithPrompt } from "./harness/session";
import { killBackendProcessOnly, spawnBackendAgainstExistingHome } from "./harness/recovery";
import { BACKEND_DIR } from "./harness/paths";
import { resolveVenvPython } from "./harness/venv";
import { buildBackendEnv, makeGroupKiller, waitUntilHealthyOrExit } from "./harness/process-utils";

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

interface SupervisedBackend {
  baseURL: string;
  /** Resolves the moment the process actually exits (real OS `exit` event,
   * not a poll) — used to confirm a graceful self-restart really terminates
   * the process rather than to arbitrarily wait out a timer. */
  exited: Promise<void>;
  stop(): Promise<void>;
}

/**
 * Spawns a backend/uvicorn process against an EXISTING FullStackBackend's
 * isolated home + port — same topology as
 * harness/recovery.ts#spawnBackendAgainstExistingHome — but with
 * `BETTER_CLAUDE_RUN_SH_SUPERVISOR=1` added to its env.
 *
 * `POST /api/admin/restart` (backend/main.py) 409s unless that env var is
 * "1": it's the guard that stops a bare "SIGTERM myself" from running with
 * nothing to bring the process back up. The `backend` fixture
 * (harness/fixtures.ts -> harness/backend.ts#startFullStackBackend) never
 * sets it, so this graceful-restart test needs its own supervisor-flagged
 * process instead of the fixture's.
 */
function spawnSupervisedBackend(backend: FullStackBackend): Promise<SupervisedBackend> {
  const python = resolveVenvPython();
  const authDir = path.join(backend.homeDir, "_auth");

  const env = {
    ...buildBackendEnv({
      homeDir: backend.homeDir,
      port: backend.port,
      username: backend.username,
      passwordHashFile: path.join(authDir, "password.hash"),
      sessionSecretFile: path.join(authDir, "session.secret"),
    }),
    BETTER_CLAUDE_RUN_SH_SUPERVISOR: "1",
  };

  const proc: ChildProcess = spawn(
    python,
    ["-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", String(backend.port)],
    { cwd: BACKEND_DIR, env, stdio: ["ignore", "pipe", "pipe"], detached: true },
  );

  const logLines: string[] = [];
  proc.stdout?.on("data", (chunk: Buffer) => logLines.push(chunk.toString()));
  proc.stderr?.on("data", (chunk: Buffer) => logLines.push(chunk.toString()));

  let exitDescription: string | null = null;
  const exited = new Promise<void>((resolve) => {
    proc.once("exit", (code, signal) => {
      exitDescription = `code=${code} signal=${signal}\n${logLines.join("")}`;
      resolve();
    });
  });

  const killGroup = makeGroupKiller(proc);
  const baseURL = backend.baseURL;

  let stopped = false;
  async function stop(): Promise<void> {
    if (stopped) return;
    stopped = true;
    if (proc.exitCode === null && proc.signalCode === null) {
      killGroup("SIGTERM");
      await Promise.race([
        exited,
        new Promise<void>((resolve) =>
          setTimeout(() => {
            killGroup("SIGKILL");
            resolve();
          }, 10_000),
        ),
      ]);
    }
  }

  return waitUntilHealthyOrExit(baseURL, () => exitDescription, 60_000, "supervised backend")
    .then(() => ({ baseURL, exited, stop }))
    .catch((err) => {
      killGroup("SIGKILL");
      throw err;
    });
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

  test("a QUEUED prompt waiting behind an in-flight turn survives a mid-turn backend crash without corruption or a duplicate send", async ({
    authedPage: page,
    backend,
  }) => {
    test.setTimeout(240_000);

    await createSessionWithPrompt(
      page,
      "Count from 1 to 100 slowly, one number per line, explaining each number's factors.",
    );

    const sessionsBeforeCrash = await getSessionsList(page, backend);
    expect(sessionsBeforeCrash.length).toBeGreaterThan(0);
    const sessionId = sessionsBeforeCrash[0].id as string;

    // Same event-driven proxy for "the first turn is actually running" as
    // chat-turn.spec.ts's queuing test.
    const stopBtn = page.getByTestId("stop-btn");
    await expect(stopBtn).toBeVisible({ timeout: 30_000 });

    // Queue a SECOND prompt behind the still-running first turn, via the
    // same same-session queue-while-running path as chat-turn.spec.ts's
    // "queues a second prompt while a turn is running" test (not the
    // offline queue) — the single primary send-btn submits it as queued
    // rather than interrupting, since the default native `claude` CLI
    // provider doesn't set `supports_steering`.
    const queuedPromptText =
      "Reply with exactly the single word: QUEUEDCRASH. No punctuation, no other words.";
    await page.getByTestId("input-textarea").fill(queuedPromptText);
    await page.getByTestId("send-btn").click();

    const queuedBanner = page.getByTestId("queued-prompt-banner");
    await expect(queuedBanner).toBeVisible({ timeout: 15_000 });
    await expect(queuedBanner).toContainText("QUEUEDCRASH");

    // Crash the backend while BOTH the running first turn AND the queued
    // second prompt are pending — the window this test targets, distinct
    // from every other test in this file which only ever has one in-flight
    // turn and nothing queued behind it.
    await killBackendProcessOnly(backend);
    const restarted = await spawnBackendAgainstExistingHome(backend);

    try {
      const health = await page.request.get(`${backend.baseURL}/api/auth/needs_setup`);
      expect(health.ok()).toBe(true);

      await page.reload();
      await page.getByTestId("chat-messages").waitFor({ state: "visible", timeout: 20_000 });

      const fetchTree = async (): Promise<{
        messages: Array<{ id: string; role: string }>;
        queued_prompts?: Array<{ kind?: string }>;
      }> => {
        const detailRes = await page.request.get(
          `${backend.baseURL}/api/sessions/${encodeURIComponent(sessionId)}?msg_limit=50`,
        );
        expect(detailRes.ok()).toBe(true);
        return detailRes.json();
      };

      // Converge to one of exactly two acceptable terminal states — never a
      // third "stuck forever" state:
      //  - "ran": the queued prompt was dequeued and actually executed as a
      //    real second turn (userCount reaches 2, matched by an equal
      //    assistantCount once that second turn also finishes).
      //  - "dropped": the queued prompt was cleanly discarded — it no
      //    longer appears in `queued_prompts` and no second user message
      //    was ever created for it.
      // Checking `is_running` on every poll (not just once up front) is
      // required because recovery may flip it false->true->false again if
      // it dequeues and runs the held prompt as a fresh turn.
      const pollOutcome = async (): Promise<"ran" | "dropped" | "pending"> => {
        const sessions = await getSessionsList(page, backend);
        const match = sessions.find((s) => s.id === sessionId);
        if (match?.is_running) return "pending";

        const tree = await fetchTree();
        const messages = tree.messages ?? [];
        const userCount = messages.filter((m) => m.role === "user").length;
        const assistantCount = messages.filter((m) => m.role === "assistant").length;
        const queuedPrompts = tree.queued_prompts ?? [];
        const stillQueued = queuedPrompts.some(
          (p) => p.kind === "queued_behind" || p.kind === "interrupt",
        );

        // Still reported as queued, or a turn's user message landed without
        // its matching assistant reply yet (settling mid-turn) — keep
        // polling rather than judging a half-written state.
        if (stillQueued || userCount !== assistantCount) return "pending";
        return userCount >= 2 ? "ran" : "dropped";
      };

      await expect
        .poll(pollOutcome, { timeout: 150_000, intervals: [500, 1000, 2000] })
        .not.toBe("pending");

      const finalTree = await fetchTree();
      const finalMessages = finalTree.messages ?? [];
      const ids = finalMessages.map((m) => m.id);
      expect(new Set(ids).size, `duplicate message ids in render tree: ${ids.join(",")}`).toBe(
        ids.length,
      );

      const userCount = finalMessages.filter((m) => m.role === "user").length;
      const assistantCount = finalMessages.filter((m) => m.role === "assistant").length;
      // Never a corrupted or duplicated send: at most the two prompts that
      // were actually submitted (the original + the queued one) can ever
      // appear as user messages.
      expect(userCount).toBeLessThanOrEqual(2);
      expect(assistantCount).toBeLessThanOrEqual(2);

      const finalQueuedPrompts = finalTree.queued_prompts ?? [];
      const stillQueuedFinal = finalQueuedPrompts.some(
        (p) => p.kind === "queued_behind" || p.kind === "interrupt",
      );
      expect(
        stillQueuedFinal,
        "queued prompt must not remain stuck reporting as queued after recovery",
      ).toBe(false);

      await page.reload();
      await page.getByTestId("chat-messages").waitFor({ state: "visible", timeout: 20_000 });

      // The queued-prompt banner is driven by `queued_prompts` from this
      // same session-detail payload (queuedPrompts.ts), so it must never
      // still show the crashed prompt as pending after reload.
      await expect(queuedBanner).toBeHidden();

      const userMessages = page.getByTestId("user-message");
      const assistantMessages = page.getByTestId("assistant-message");

      if (userCount >= 2) {
        // Outcome: the queued prompt got a real reply eventually — a
        // genuine second turn, not a duplicate of the first.
        await expect(userMessages).toHaveCount(2);
        await expect(assistantMessages).toHaveCount(2, { timeout: 30_000 });
        await expect(assistantMessages.nth(1)).toContainText("QUEUEDCRASH", { timeout: 120_000 });
      } else {
        // Outcome: the queued prompt was cleanly dropped — exactly the
        // original turn remains, with no phantom second bubble.
        await expect(userMessages).toHaveCount(1);
        await expect(assistantMessages).toHaveCount(1);
      }

      // Whichever outcome occurred, the FIRST turn's bubble must show a
      // real terminal signal too (real content or a stopped indicator),
      // same invariant as the other mid-turn-crash tests in this file.
      const firstAssistantMessage = assistantMessages.first();
      const firstText = (await firstAssistantMessage.textContent()) ?? "";
      const firstHasRealContent = firstText.trim().length > 0;
      const firstHasStoppedIndicator =
        (await firstAssistantMessage.locator(".stopped-indicator").count()) > 0;
      expect(
        firstHasRealContent || firstHasStoppedIndicator,
        `first assistant bubble has neither content nor a stopped indicator after recovery: ${JSON.stringify(firstText)}`,
      ).toBe(true);
    } finally {
      await restarted.stop();
    }
  });
});

test.describe("backend graceful-restart (POST /api/admin/restart)", () => {
  test("render tree stays consistent after a graceful self-restart via POST /api/admin/restart", async ({
    authedPage: page,
    backend,
  }) => {
    test.setTimeout(180_000);

    // The `backend` fixture's process never sets
    // BETTER_CLAUDE_RUN_SH_SUPERVISOR=1, so /api/admin/restart would just
    // 409 on it (backend/main.py fails closed without the run.sh
    // supervisor). Swap it for a supervisor-flagged process against the
    // SAME isolated home/port before creating any session, so the swap
    // itself has no bearing on the render-tree assertions below.
    await killBackendProcessOnly(backend);
    const supervised = await spawnSupervisedBackend(backend);

    try {
      await page.reload();
      await page.locator(".session-new-button").waitFor({ state: "visible", timeout: 20_000 });

      await createSessionWithPrompt(
        page,
        "Count from 1 to 40, one number per line, then write a short paragraph " +
          "explaining why counting is a foundational skill. Do not rush, be thorough.",
      );
      const sessionId = new URL(page.url()).pathname.replace(/^\/s\//, "");

      // Wait until the real provider CLI turn is actually in flight before
      // triggering the restart, exercising the same mid-turn window the
      // SIGKILL crash tests cover — but through the app's own intentional
      // shutdown path (`on_shutdown`, backend/main.py) instead of an
      // abrupt process kill. That path runs real cleanup (model catalog
      // refresh shutdown, lag-incident-queue stop, marketplace bridge
      // stop, OAuth-subprocess shutdown, extension daemon shutdown, etc.)
      // a SIGKILL never gets a chance to run, while still leaving the
      // orphaned provider CLI runner alive for recovery, exactly like a
      // crash (SIGTERM, not SIGINT, so `_intentional_shutdown` stays
      // false and `provider.cancel_all` is skipped).
      await waitForRunning(page, backend, sessionId, 30_000);

      const restartRes = await page.request.post(`${supervised.baseURL}/api/admin/restart`, {
        data: { mode: "now" },
      });
      expect(
        restartRes.ok(),
        `POST /api/admin/restart failed: ${restartRes.status()} ${await restartRes.text()}`,
      ).toBe(true);

      // `_trigger_supervisor_restart` (backend/main.py) SIGTERMs the
      // process itself ~0.3s after answering, on the assumption a run.sh
      // supervisor is watching to respawn it. This harness runs no such
      // supervisor, so the process must actually exit and NOT come back on
      // its own — confirm that real exit (a real OS `exit` event, not a
      // fixed sleep) before bringing it back up manually, same as the
      // crash tests.
      await Promise.race([
        supervised.exited,
        new Promise((_resolve, reject) =>
          setTimeout(
            () => reject(new Error("backend did not exit after POST /api/admin/restart within 20s")),
            20_000,
          ),
        ),
      ]);

      const restarted = await spawnBackendAgainstExistingHome(backend);
      try {
        const health = await page.request.get(`${backend.baseURL}/api/auth/needs_setup`);
        expect(health.ok()).toBe(true);

        await page.reload();
        await page.getByTestId("chat-messages").waitFor({ state: "visible", timeout: 20_000 });

        // Give recovery/reconcile the same generous convergence window as
        // the crash tests: either the orphaned real provider CLI process
        // finished the turn on its own, or the backend detected it was
        // dead/gone and finalized the turn as stopped. Either is
        // acceptable — stuck "running" forever is not.
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
          `assistant bubble has neither content nor a stopped indicator after graceful-restart recovery: ${JSON.stringify(text)}`,
        ).toBe(true);
      } finally {
        await restarted.stop();
      }
    } finally {
      await supervised.stop();
    }
  });
});
