import { describe, expect, it } from "vitest";

import { parseCommonItemDraft, parseJsonObject, parseMcpServers } from "@better-agent/provider-config-sync-core/items";

describe("providerConfigSyncItems", () => {
  it("parses MCP servers into editable fields", () => {
    const servers = parseMcpServers(JSON.stringify({
      mcpServers: {
        demo: {
          command: "node",
          args: ["server.js", "--debug"],
          env: { TOKEN: "x" },
          disabled: false,
        },
      },
    }));

    expect(servers).toEqual([
      {
        name: "demo",
        command: "node",
        args: "server.js\n--debug",
        env: '{\n  "TOKEN": "x"\n}',
        extra: '{\n  "disabled": false\n}',
      },
    ]);
  });

  it("parses common agent/skill item fields", () => {
    const item = parseCommonItemDraft(JSON.stringify({
      name: "reviewer",
      description: "Review code",
      instructions: "Read carefully.\n",
      metadata: { model: "sonnet" },
    }));

    expect(item).toEqual({
      name: "reviewer",
      description: "Review code",
      instructions: "Read carefully.\n",
      metadata: '{\n  "model": "sonnet"\n}',
    });
  });

  it("rejects non-object JSON roots", () => {
    expect(parseJsonObject("[]")).toBeNull();
    expect(parseJsonObject("not json")).toBeNull();
  });
});
