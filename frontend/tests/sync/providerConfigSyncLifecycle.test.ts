import { afterEach, describe, expect, it, vi } from "vitest";
import {
  createFetchProviderConfigSyncClient,
  type ProviderConfigSyncMutationContext,
} from "../../../provider-config-sync/packages/provider-config-sync-ui/src/client";

describe("provider config sync mutation lifecycle", () => {
  afterEach(() => vi.restoreAllMocks());

  it("supplies resource identity, authoritative refetch/predicate, and reconciliation", async () => {
    const state = { groups: {}, providers: [] };
    globalThis.fetch = vi.fn()
      .mockResolvedValueOnce(new Response(null, { status: 204 }))
      .mockImplementation(() => Promise.resolve(new Response(JSON.stringify(state), { status: 200 })));
    const contexts: ProviderConfigSyncMutationContext[] = [];
    const client = createFetchProviderConfigSyncClient({
      baseUrl: "",
      runMutation: async (context, mutate) => {
        contexts.push(context);
        const result = await mutate();
        const authoritative = await context.refetch();
        expect(context.isAuthoritative(authoritative)).toBe(true);
        await context.reconcile();
        return result;
      },
    });

    await client.writeFile({
      cwd: "/project",
      entry_id: "instructions",
      expected_content: null,
      content: "updated",
    });

    expect(contexts).toHaveLength(1);
    expect(contexts[0]).toMatchObject({
      operation: "write-file",
      resourceKey: "/project:instructions",
    });
    expect(globalThis.fetch).toHaveBeenCalledTimes(3);
    expect(String(vi.mocked(globalThis.fetch).mock.calls[1][0])).toContain("cwd=%2Fproject");
  });
});
