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
});
