import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { test, expect } from "./harness/fixtures";
import {
  killBackendProcessOnly,
  spawnBackendAgainstExistingHome,
} from "./harness/recovery";

test("renders journaled Codex children after backend restart and browser reload", async ({
  authedPage: page,
  backend,
}) => {
  const create = await page.request.post(`${backend.baseURL}/api/sessions`, {
    data: { name: "Codex child rendering", cwd: "/tmp" },
  });
  expect(create.ok()).toBe(true);
  const session = (await create.json()) as { id: string };

  await killBackendProcessOnly(backend);
  const sessionPath = path.join(backend.homeDir, "sessions", `${session.id}.json`);
  const tree = JSON.parse(readFileSync(sessionPath, "utf-8")) as Record<string, unknown>;
  const userMessageId = "u-codex-children";
  const assistantMessageId = "a-codex-children";
  tree.messages = [
    {
      id: userMessageId,
      role: "user",
      content: "coordinate children",
      events: [],
      timestamp: "2026-08-04T09:00:00.000Z",
      isStreaming: false,
    },
    {
      id: assistantMessageId,
      role: "assistant",
      content: "parent final",
      events: [],
      workers: [],
      timestamp: "2026-08-04T09:00:01.000Z",
      isStreaming: false,
      run_meta: { provider_id: "codex", model: "gpt-5.6-sol" },
    },
  ];
  tree.last_applied_seq = 0;
  writeFileSync(sessionPath, JSON.stringify(tree));

  const eventsDir = path.join(backend.homeDir, "sessions", session.id);
  mkdirSync(eventsDir, { recursive: true });
  const childRows = [
    ["child-a", "First Codex child", "first child output"],
    ["child-b", "Second Codex child", "second child output"],
  ].flatMap(([delegationId, label, output], childIndex) => {
    const firstSeq = childIndex * 3 + 1;
    return [
      {
        seq: firstSeq,
        type: "worker_start",
        data: {
          delegation_id: delegationId,
          worker_session_id: `codex-thread-${delegationId}`,
          worker_description: label,
          panel_kind: "worker",
          run_mode: "codex_subagent",
          is_new: false,
        },
      },
      {
        seq: firstSeq + 1,
        type: "worker_event",
        data: {
          delegation_id: delegationId,
          event: {
            type: "agent_message",
            data: {
              uuid: `${delegationId}-output`,
              type: "assistant",
              message: { content: [{ type: "text", text: output }] },
            },
          },
        },
      },
      {
        seq: firstSeq + 2,
        type: "worker_complete",
        data: { delegation_id: delegationId, success: true },
      },
    ];
  }).map((row) => ({
    ...row,
    ts: "2026-08-04T09:00:02.000Z",
    root_id: session.id,
    sid: session.id,
    msg_id: assistantMessageId,
    source: "provider_stream",
  }));
  writeFileSync(
    path.join(eventsDir, "events.jsonl"),
    `${childRows.map((row) => JSON.stringify(row)).join("\n")}\n`,
  );

  const restarted = await spawnBackendAgainstExistingHome(backend);
  try {
    const restoredResponse = await page.request.get(
      `${backend.baseURL}/api/sessions/${session.id}`,
    );
    expect(restoredResponse.ok()).toBe(true);
    const restoredTree = await restoredResponse.json() as {
      messages: Array<{ id: string; workers?: unknown[] }>;
    };
    const restoredAssistant = restoredTree.messages.find(
      (message) => message.id === assistantMessageId,
    );
    expect(
      restoredAssistant?.workers,
      JSON.stringify(restoredAssistant),
    ).toHaveLength(2);

    await page.goto(`${backend.baseURL}/s/${session.id}`);
    await page.locator(".message-box-header-main").first().click();
    for (const label of ["First Codex child", "Second Codex child"]) {
      await expect(page.getByRole("button", { name: new RegExp(label, "i") })).toBeVisible();
    }
    await page.getByRole("button", { name: /First Codex child/i }).click();
    await expect(page.getByText("first child output")).toBeVisible();
    await expect(page.locator(".assistant-run-meta-footer.is-running")).toHaveCount(0);

    const internalToken = readFileSync(
      path.join(backend.homeDir, "internal_token"),
      "utf-8",
    ).trim();
    const liveEvents = [
      {
        type: "worker_start",
        data: {
          delegation_id: "child-live",
          worker_session_id: "codex-thread-child-live",
          worker_description: "Live Codex child",
          panel_kind: "worker",
          run_mode: "codex_subagent",
          is_new: false,
        },
      },
      {
        type: "worker_event",
        data: {
          delegation_id: "child-live",
          event: {
            type: "agent_message",
            data: {
              uuid: "child-live-output",
              type: "assistant",
              message: { content: [{ type: "text", text: "live child output" }] },
            },
          },
        },
      },
      {
        type: "worker_complete",
        data: { delegation_id: "child-live", success: true },
      },
    ];
    for (const event of liveEvents) {
      const projected = await page.request.post(
        `${backend.baseURL}/api/internal/test/project-worker-event`,
        {
          headers: { "X-Internal-Token": internalToken },
          data: {
            confirm: "PROJECT_WORKER_EVENT_FOR_TESTING",
            session_id: session.id,
            msg_id: assistantMessageId,
            ...event,
          },
        },
      );
      expect(projected.ok(), await projected.text()).toBe(true);
    }
    const liveChild = page.getByRole("button", { name: /Live Codex child/i });
    await expect(liveChild).toBeVisible();
    await liveChild.click();
    await expect(page.getByText("live child output")).toBeVisible();

    await page.reload();
    await page.locator(".message-box-header-main").first().click();
    await expect(page.getByRole("button", { name: /First Codex child/i })).toBeVisible();
    await expect(page.getByRole("button", { name: /Second Codex child/i })).toBeVisible();
    await expect(page.getByRole("button", { name: /Live Codex child/i })).toBeVisible();
    await expect(page.locator(".assistant-run-meta-footer.is-running")).toHaveCount(0);
  } finally {
    await restarted.stop();
  }
});
