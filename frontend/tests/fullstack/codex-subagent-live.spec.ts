import { existsSync, readFileSync, readdirSync, writeFileSync } from "node:fs";
import path from "node:path";
import {
  test as base,
  expect,
  type Page,
  type TestInfo,
} from "@playwright/test";
import {
  startFullStackBackend,
  type FullStackBackend,
} from "./harness/backend";
import { loginAndSkipFirstRunWizard } from "./harness/login";
import {
  createSessionWithoutTurn,
  createSessionWithPrompt,
} from "./harness/session";

interface LiveFixtures {
  backend: FullStackBackend;
  authedPage: Page;
}

interface Evidence {
  caseName: string;
  capturedAt: string;
  sessionId: string;
  apiStatus: number;
  model: string | null;
  providerId: string | null;
  isRunning: boolean | null;
  workers: Array<{
    delegationId: string;
    workerSessionId: string;
    description: string;
    parentDelegationId: string | null;
    success: boolean | null;
    error: string | null;
    eventCount: number;
  }>;
  journal: {
    workerStarts: number;
    workerEvents: number;
    workerCompletes: number;
    delegationIds: string[];
    terminalByDelegation: Record<string, boolean | null>;
  };
  runs: Array<{
    runId: string;
    model: string | null;
    providerSessionId: string | null;
    rolloutFile: string | null;
    complete: boolean;
    success: boolean | null;
    error: string | null;
  }>;
  dom: {
    workerPanels: number;
    runningIndicators: number;
    stoppedIndicators: number;
  };
  screenshot: string;
  captureError?: string;
}

const MODEL = "gpt-5.6-sol";
const LIVE_ENABLED = process.env.RUN_LLM_TESTS === "1";
const test = base.extend<LiveFixtures>({
  backend: async ({}, use, testInfo) => {
    const backend = await startFullStackBackend({ provider: "codex" });
    try {
      await use(backend);
    } finally {
      if (testInfo.status !== testInfo.expectedStatus) {
        await testInfo.attach("backend-log", {
          body: backend.logs(),
          contentType: "text/plain",
        });
      }
      await backend.stop();
    }
  },
  authedPage: async ({ page, backend }, use) => {
    await loginAndSkipFirstRunWizard(page, backend);
    await use(page);
  },
});

test.describe.configure({ timeout: 600_000 });
const liveTest = LIVE_ENABLED ? test : test.skip;

function readJson(pathname: string): Record<string, unknown> {
  try {
    return JSON.parse(readFileSync(pathname, "utf-8")) as Record<string, unknown>;
  } catch {
    return {};
  }
}

function journalRows(backend: FullStackBackend, sessionId: string): Array<Record<string, unknown>> {
  const journal = path.join(backend.homeDir, "sessions", sessionId, "events.jsonl");
  if (!existsSync(journal)) return [];
  return readFileSync(journal, "utf-8")
    .split("\n")
    .filter(Boolean)
    .flatMap((line) => {
      try {
        return [JSON.parse(line) as Record<string, unknown>];
      } catch {
        return [];
      }
    });
}

function runFacts(backend: FullStackBackend, sessionId: string): Evidence["runs"] {
  const runsRoot = path.join(backend.homeDir, "runs");
  if (!existsSync(runsRoot)) return [];
  return readdirSync(runsRoot).flatMap((runId) => {
    const runDir = path.join(runsRoot, runId);
    const input = readJson(path.join(runDir, "input.json"));
    if (input.app_session_id !== sessionId) return [];
    const state = readJson(path.join(runDir, "state.json"));
    const complete = readJson(path.join(runDir, "complete.json"));
    const rolloutPath = typeof state.rollout_path === "string" ? state.rollout_path : null;
    return [{
      runId,
      model: typeof input.model === "string" ? input.model : null,
      providerSessionId: typeof state.session_id === "string" ? state.session_id : null,
      rolloutFile: rolloutPath ? path.basename(rolloutPath) : null,
      complete: Object.keys(complete).length > 0,
      success: typeof complete.success === "boolean" ? complete.success : null,
      error: typeof complete.error === "string" ? complete.error.slice(0, 500) : null,
    }];
  });
}

async function captureEvidence(
  page: Page,
  backend: FullStackBackend,
  sessionId: string,
  caseName: string,
  testInfo: TestInfo,
): Promise<Evidence> {
  const screenshot = testInfo.outputPath(`${caseName}.png`);
  const evidencePath = testInfo.outputPath(`${caseName}.json`);
  let evidence: Evidence;
  try {
    const response = await page.request.get(`${backend.baseURL}/api/sessions/${sessionId}`);
    const tree = response.ok() ? await response.json() as Record<string, unknown> : {};
    const messages = Array.isArray(tree.messages) ? tree.messages as Array<Record<string, unknown>> : [];
    const assistant = [...messages].reverse().find((message) => message.role === "assistant") ?? {};
    const workers = Array.isArray(assistant.workers)
      ? assistant.workers as Array<Record<string, unknown>>
      : [];
    const rows = journalRows(backend, sessionId);
    const lifecycle = rows.filter((row) =>
      ["worker_start", "worker_event", "worker_complete"].includes(String(row.type)),
    );
    const terminalByDelegation: Record<string, boolean | null> = {};
    for (const row of lifecycle) {
      if (row.type !== "worker_complete") continue;
      const data = row.data as Record<string, unknown> | undefined;
      const delegationId = typeof data?.delegation_id === "string" ? data.delegation_id : "";
      if (!delegationId) continue;
      terminalByDelegation[delegationId] = typeof data?.success === "boolean" ? data.success : null;
    }
    await page.screenshot({ path: screenshot, fullPage: false });
    evidence = {
      caseName,
      capturedAt: new Date().toISOString(),
      sessionId,
      apiStatus: response.status(),
      model: typeof assistant.run_meta === "object" && assistant.run_meta
        ? String((assistant.run_meta as Record<string, unknown>).model ?? "") || null
        : typeof tree.model === "string" ? tree.model : null,
      providerId: typeof assistant.run_meta === "object" && assistant.run_meta
        ? String((assistant.run_meta as Record<string, unknown>).provider_id ?? "") || null
        : typeof tree.provider_id === "string" ? tree.provider_id : null,
      isRunning: typeof tree.is_running === "boolean" ? tree.is_running : null,
      workers: workers.map((worker) => ({
        delegationId: String(worker.delegation_id ?? ""),
        workerSessionId: String(worker.worker_session_id ?? ""),
        description: String(worker.worker_description ?? ""),
        parentDelegationId: typeof worker.parent_delegation_id === "string"
          ? worker.parent_delegation_id
          : null,
        success: typeof worker.success === "boolean" ? worker.success : null,
        error: typeof worker.error === "string" ? worker.error.slice(0, 500) : null,
        eventCount: Array.isArray(worker.events) ? worker.events.length : 0,
      })),
      journal: {
        workerStarts: lifecycle.filter((row) => row.type === "worker_start").length,
        workerEvents: lifecycle.filter((row) => row.type === "worker_event").length,
        workerCompletes: lifecycle.filter((row) => row.type === "worker_complete").length,
        delegationIds: [...new Set(lifecycle.flatMap((row) => {
          const data = row.data as Record<string, unknown> | undefined;
          return typeof data?.delegation_id === "string" ? [data.delegation_id] : [];
        }))],
        terminalByDelegation,
      },
      runs: runFacts(backend, sessionId),
      dom: {
        workerPanels: await page.locator(".collapsible-timeline-block:has(.role-chip-worker)").count(),
        runningIndicators: await page.locator(".turn-group.is-running, .assistant-run-meta-footer.is-running").count(),
        stoppedIndicators: await page.locator(".stopped-indicator").count(),
      },
      screenshot,
    };
  } catch (error) {
    evidence = {
      caseName,
      capturedAt: new Date().toISOString(),
      sessionId,
      apiStatus: 0,
      model: null,
      providerId: null,
      isRunning: null,
      workers: [],
      journal: {
        workerStarts: 0,
        workerEvents: 0,
        workerCompletes: 0,
        delegationIds: [],
        terminalByDelegation: {},
      },
      runs: runFacts(backend, sessionId),
      dom: { workerPanels: 0, runningIndicators: 0, stoppedIndicators: 0 },
      screenshot,
      captureError: error instanceof Error ? error.message : String(error),
    };
  }
  writeFileSync(evidencePath, `${JSON.stringify(evidence, null, 2)}\n`);
  await testInfo.attach(`${caseName}-evidence`, {
    path: evidencePath,
    contentType: "application/json",
  });
  if (existsSync(screenshot)) {
    await testInfo.attach(`${caseName}-screenshot`, { path: screenshot, contentType: "image/png" });
  }
  return evidence;
}

async function startCodexTurn(page: Page, prompt: string): Promise<string> {
  const session = await createSessionWithPrompt(page, prompt, {
    modelPattern: /^gpt-5\.6-sol$/i,
  });
  return session.id;
}

async function waitForMarker(page: Page, marker: string): Promise<void> {
  await expect(
    page.getByTestId("assistant-message").filter({ hasText: marker }).last(),
  ).toContainText(marker, { timeout: 360_000 });
  const group = page.locator(".turn-group").filter({ hasText: marker }).last();
  const toggle = group.locator(".message-box-header-main").first();
  if (await toggle.getAttribute("aria-expanded") === "false") await toggle.click();
}

async function waitForLifecycle(
  backend: FullStackBackend,
  sessionId: string,
  workerCount: number,
): Promise<void> {
  await expect.poll(() => {
    const rows = journalRows(backend, sessionId);
    return {
      starts: rows.filter((row) => row.type === "worker_start").length,
      completes: rows.filter((row) => row.type === "worker_complete").length,
    };
  }, { timeout: 45_000 }).toEqual({ starts: workerCount, completes: workerCount });
}

function assertCompletedEvidence(evidence: Evidence, workerCount: number): void {
  expect(evidence.apiStatus).toBe(200);
  expect(evidence.model).toBe(MODEL);
  expect(evidence.providerId).toBeTruthy();
  expect(evidence.workers).toHaveLength(workerCount);
  expect(evidence.journal.workerStarts).toBe(workerCount);
  expect(evidence.journal.workerCompletes).toBe(workerCount);
  expect(evidence.journal.workerEvents).toBeGreaterThanOrEqual(workerCount);
  expect(evidence.journal.delegationIds).toHaveLength(workerCount);
  expect(evidence.workers.every((worker) => worker.success === true)).toBe(true);
  expect(evidence.runs.some((run) =>
    run.model === MODEL
    && run.providerSessionId
    && run.rolloutFile
    && run.complete
    && run.success === true,
  )).toBe(true);
  expect(evidence.dom.workerPanels).toBe(workerCount);
  expect(evidence.dom.runningIndicators).toBe(0);
}

async function runCompletedCase(
  page: Page,
  backend: FullStackBackend,
  testInfo: TestInfo,
  caseName: string,
  prompt: string,
  marker: string,
  workerCount: number,
  extraAssertion?: (evidence: Evidence) => void,
): Promise<void> {
  let sessionId = "";
  let captured = false;
  try {
    sessionId = await startCodexTurn(page, prompt);
    await waitForMarker(page, marker);
    await expect(page.locator(".collapsible-timeline-block:has(.role-chip-worker)")).toHaveCount(
      workerCount,
      { timeout: 30_000 },
    );
    await waitForLifecycle(backend, sessionId, workerCount);
    const evidence = await captureEvidence(page, backend, sessionId, caseName, testInfo);
    captured = true;
    assertCompletedEvidence(evidence, workerCount);
    extraAssertion?.(evidence);
  } finally {
    if (sessionId && !captured) {
      await captureEvidence(page, backend, sessionId, caseName, testInfo);
    }
  }
}

test("preflight selects exact Codex model without a provider turn", async ({ authedPage: page }) => {
  const session = await createSessionWithoutTurn(page, { modelPattern: /^gpt-5\.6-sol$/i });
  expect(session.model).toBe(MODEL);
});

test("preflight rejects a missing explicit model without a provider turn", async ({ authedPage: page }) => {
  await expect(
    createSessionWithoutTurn(page, { modelPattern: /^model-that-does-not-exist$/i }),
  ).rejects.toThrow();
});

liveTest("1 single Codex child renders and completes", async ({ authedPage: page, backend }, testInfo) => {
  await runCompletedCase(
    page,
    backend,
    testInfo,
    "01-single-child",
    "Use spawn_agent exactly once with task_name live_single_child. Tell it to reply exactly CHILD_SINGLE_OK. Wait for it, then reply exactly ROOT_SINGLE_OK. Do not spawn any other agents.",
    "ROOT_SINGLE_OK",
    1,
  );
});

liveTest("2 sibling Codex children both render and complete", async ({ authedPage: page, backend }, testInfo) => {
  await runCompletedCase(
    page,
    backend,
    testInfo,
    "02-sibling-children",
    "Spawn exactly two agents, live_sibling_a and live_sibling_b, before waiting. Tell A to reply exactly CHILD_A_OK and B exactly CHILD_B_OK. Wait for both, then reply exactly ROOT_SIBLINGS_OK. Do not spawn other agents.",
    "ROOT_SIBLINGS_OK",
    2,
  );
});

liveTest("3 nested Codex grandchild preserves hierarchy", async ({ authedPage: page, backend }, testInfo) => {
  await runCompletedCase(
    page,
    backend,
    testInfo,
    "03-nested-child",
    "Spawn exactly one agent named live_nested_parent. Tell that child to spawn exactly one agent named live_nested_grandchild, wait for it to reply GRANDCHILD_OK, then reply CHILD_NESTED_OK. Wait for the child, then reply exactly ROOT_NESTED_OK. Do not spawn any other agents.",
    "ROOT_NESTED_OK",
    2,
    (evidence) => {
      expect(evidence.workers.filter((worker) => worker.parentDelegationId)).toHaveLength(1);
    },
  );
});

liveTest("4 active Codex child remains visibly running across reload", async ({ authedPage: page, backend }, testInfo) => {
  let sessionId = "";
  let captured = false;
  try {
    sessionId = await startCodexTurn(
      page,
      "Spawn exactly one agent named live_reload_child. Tell it to produce 800 numbered one-sentence observations about software testing, then end with CHILD_RELOAD_OK. Wait for it, then reply exactly ROOT_RELOAD_OK. Do not spawn other agents.",
    );
    await expect(page.locator(".collapsible-timeline-block:has(.role-chip-worker)")).toHaveCount(1, {
      timeout: 180_000,
    });
    await expect(page.getByTestId("stop-btn")).toBeVisible();
    await page.reload();
    await expect(page.getByTestId("stop-btn")).toBeVisible({ timeout: 30_000 });
    await expect(page.locator(".turn-group.is-running")).toBeVisible();
    await waitForMarker(page, "ROOT_RELOAD_OK");
    await waitForLifecycle(backend, sessionId, 1);
    const evidence = await captureEvidence(page, backend, sessionId, "04-active-reload", testInfo);
    captured = true;
    assertCompletedEvidence(evidence, 1);
  } finally {
    if (sessionId && !captured) {
      await captureEvidence(page, backend, sessionId, "04-active-reload", testInfo);
    }
  }
});

liveTest("5 cancelling a live Codex child projects terminal stopped state", async ({ authedPage: page, backend }, testInfo) => {
  let sessionId = "";
  let captured = false;
  try {
    sessionId = await startCodexTurn(
      page,
      "Spawn exactly one agent named live_cancel_child. Tell it to produce 2000 numbered one-sentence observations, ending with CHILD_CANCEL_SHOULD_NOT_FINISH. Wait for it, then reply ROOT_CANCEL_SHOULD_NOT_FINISH. Do not spawn other agents.",
    );
    await expect(page.locator(".collapsible-timeline-block:has(.role-chip-worker)")).toHaveCount(1, {
      timeout: 180_000,
    });
    const stop = page.getByTestId("stop-btn");
    await expect(stop).toBeVisible();
    await stop.click();
    await expect(stop).toBeHidden({ timeout: 30_000 });
    await expect(page.locator(".stopped-indicator")).toBeVisible({ timeout: 30_000 });
    await waitForLifecycle(backend, sessionId, 1);
    const evidence = await captureEvidence(page, backend, sessionId, "05-cancel-child", testInfo);
    captured = true;
    expect(evidence.apiStatus).toBe(200);
    expect(evidence.model).toBe(MODEL);
    expect(evidence.providerId).toBeTruthy();
    expect(evidence.workers).toHaveLength(1);
    expect(evidence.journal.workerStarts).toBe(1);
    expect(evidence.journal.workerCompletes).toBe(1);
    expect(Object.values(evidence.journal.terminalByDelegation)).toEqual([false]);
    expect(evidence.workers[0].success).toBe(false);
    expect(evidence.dom.runningIndicators).toBe(0);
    expect(evidence.dom.stoppedIndicators).toBeGreaterThanOrEqual(1);
  } finally {
    if (sessionId && !captured) {
      await captureEvidence(page, backend, sessionId, "05-cancel-child", testInfo);
    }
  }
});
