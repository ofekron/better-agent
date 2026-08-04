import { test, expect } from "./harness/fixtures";

test("renders nested Codex child activity and terminal state across reload", async ({
  authedPage: page,
  backend,
}) => {
  const create = await page.request.post(`${backend.baseURL}/api/sessions`, {
    data: { name: "Codex child rendering", cwd: "/tmp" },
  });
  expect(create.ok()).toBe(true);
  const session = (await create.json()) as { id: string };
  let running = true;

  const child = (
    id: string,
    label: string,
    success?: boolean,
    parentDelegationId?: string,
  ) => ({
    delegation_id: id,
    worker_session_id: `thread-${id}`,
    worker_description: label,
    panel_kind: "worker",
    run_mode: "codex_subagent",
    parent_delegation_id: parentDelegationId,
    is_new: false,
    instructions_preview: "",
    events: [{ type: "output", data: { output: `${label} output` } }],
    ...(success === undefined ? {} : { success }),
  });

  await page.route(`**/api/sessions/${encodeURIComponent(session.id)}*`, async (route) => {
    const response = await route.fetch();
    const tree = (await response.json()) as Record<string, unknown>;
    tree.messages = [
      {
        id: "u-codex-children",
        role: "user",
        content: "coordinate children",
        events: [],
        timestamp: "2026-08-04T09:00:00.000Z",
        isStreaming: false,
      },
      {
        id: "a-codex-children",
        role: "assistant",
        content: running ? "parent-only status" : "parent-only final",
        events: [],
        timestamp: "2026-08-04T09:00:01.000Z",
        isStreaming: running,
        run_meta: { provider_id: "codex", model: "gpt-5.6" },
        workers: [
          {
            ...child("codex-root", "Active Codex child", running ? undefined : true),
            jsonl_path: "/tmp/sanitized-codex-child.jsonl",
            new_byte_offset: 42,
          },
          child("codex-done", "Completed Codex child", true),
          child("codex-failed", "Failed Codex child", false),
          child(
            "codex-nested",
            "Nested Codex child",
            running ? undefined : true,
            "codex-root",
          ),
        ],
      },
    ];
    tree.total_messages = 2;
    await route.fulfill({ response, body: JSON.stringify(tree) });
  });

  await page.goto(new URL(`/s/${session.id}`, backend.baseURL).toString());
  await expect(page.getByTestId("assistant-message")).toBeVisible();

  const expanded = async (label: RegExp) =>
    page.getByRole("button", { name: label }).getAttribute("aria-expanded");
  await expect.poll(() => expanded(/Active Codex child/i)).toBe("true");
  await expect.poll(() => expanded(/Nested Codex child/i)).toBe("true");
  await expect.poll(() => expanded(/Completed Codex child/i)).toBe("false");
  await expect.poll(() => expanded(/Failed Codex child/i)).toBe("false");
  await expect(page.locator(".assistant-run-meta-footer.is-running")).toBeVisible();

  running = false;
  await page.reload();
  const summary = page.locator(".collapse-summary");
  await expect(summary).toHaveText("4 workers");
  await expect(summary).not.toContainText("Codex child output");
  await page.locator(".message-box-header-main").click();
  await expect(page.getByTestId("assistant-message")).toContainText("parent-only final");

  await expect.poll(() => expanded(/Active Codex child/i)).toBe("false");
  await expect.poll(() => expanded(/Nested Codex child/i)).toBe("false");
  await expect(page.locator(".assistant-run-meta-footer.is-running")).toHaveCount(0);
});
