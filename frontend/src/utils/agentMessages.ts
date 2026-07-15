import type { WSEvent } from "../types";

export type FlatEventsResult = {
  flat: WSEvent[];
  toolResultById: Map<string, string>;
};

function toolResultContentToString(content: unknown): string {
  if (content == null) return "";
  if (typeof content === "string") return content;
  if (Array.isArray(content)) {
    const parts: string[] = [];
    for (const block of content) {
      if (block && typeof block === "object") {
        const b = block as { type?: string; text?: string };
        if (b.type === "text" && typeof b.text === "string") {
          parts.push(b.text);
        } else {
          try {
            parts.push(JSON.stringify(block));
          } catch {
            parts.push(String(block));
          }
        }
      } else if (typeof block === "string") {
        parts.push(block);
      }
    }
    return parts.join("\n");
  }
  try {
    return JSON.stringify(content);
  } catch {
    return String(content);
  }
}

function normalizeToolName(name: string): string {
  if (name.startsWith("mcp__") && name.endsWith("__delegate")) return "delegate";
  return name;
}

function jsonObjectFromString(text: string): Record<string, unknown> | null {
  try {
    const parsed = JSON.parse(text);
    return parsed && typeof parsed === "object" && !Array.isArray(parsed)
      ? parsed as Record<string, unknown>
      : null;
  } catch {
    return null;
  }
}

function textBlockFromAgentMessage(ev: WSEvent): string | null {
  if (ev.type !== "agent_message") return null;
  const data = ev.data as Record<string, unknown> | undefined;
  if (!data || data.type !== "assistant") return null;
  const message = data.message as Record<string, unknown> | undefined;
  const content = message?.content;
  if (!Array.isArray(content) || content.length !== 1) return null;
  const block = content[0] as Record<string, unknown> | undefined;
  if (!block || block.type !== "text" || typeof block.text !== "string") return null;
  return block.text;
}

function duplicateCodexNativeAgentMessage(rawText: string, renderedTexts: Set<string>): boolean {
  const prefix = "Codex native event_msg.agent_message\n\n```json\n";
  if (!rawText.startsWith(prefix) || !rawText.endsWith("\n```")) return false;
  const body = rawText.slice(prefix.length, -"\n```".length);
  try {
    const payload = JSON.parse(body) as { message?: unknown };
    return typeof payload.message === "string" && renderedTexts.has(payload.message);
  } catch {
    return false;
  }
}

function durationText(durationMs: unknown): string | null {
  if (typeof durationMs !== "number" || !Number.isInteger(durationMs) || durationMs < 1000) {
    return null;
  }
  const totalSeconds = Math.round(durationMs / 1000);
  const seconds = totalSeconds % 60;
  const totalMinutes = Math.floor(totalSeconds / 60);
  const minutes = totalMinutes % 60;
  const hours = Math.floor(totalMinutes / 60);
  if (hours > 0) return `${hours}h ${minutes}m ${seconds}s`;
  if (minutes > 0) return `${minutes}m ${seconds}s`;
  return `${seconds}s`;
}

function codexNativeNoticeText(payload: { type?: unknown; reason?: unknown; duration_ms?: unknown }): string | null {
  if (payload.type === "context_compacted") return "Context compacted";
  if (payload.type === "turn_aborted") {
    const text = payload.reason === "interrupted" ? "Turn interrupted" : "Turn aborted";
    const duration = durationText(payload.duration_ms);
    return duration ? `${text} after ${duration}` : text;
  }
  return null;
}

function codexNativeFallbackLifecycleNotice(rawText: string): WSEvent | null {
  const match = rawText.match(/^Codex native event_msg\.([^\n]+)\n\n```json\n/);
  if (!match || !rawText.endsWith("\n```")) return null;
  const prefix = match[0];
  const body = rawText.slice(prefix.length, -"\n```".length);
  try {
    const payload = JSON.parse(body) as { type?: unknown; reason?: unknown; duration_ms?: unknown };
    const message = codexNativeNoticeText(payload);
    if (!message) return null;
    return {
      type: "lifecycle_notice",
      data: {
        kind: typeof payload.type === "string" ? payload.type : "codex_event",
        message,
        reason: payload.reason,
        duration_ms: payload.duration_ms,
      },
    } as WSEvent;
  } catch {
    return null;
  }
}

function deriveTs(ev: WSEvent): string | undefined {
  if (ev._ts) return ev._ts;
  const d = ev.data as Record<string, unknown> | undefined;
  if (!d) return undefined;
  if (typeof d.timestamp === "string") return d.timestamp;
  const msg = d.message as Record<string, unknown> | undefined;
  if (msg && typeof msg.timestamp === "string") return msg.timestamp;
  return undefined;
}

export function flattenClaudeMessages(events: WSEvent[]): FlatEventsResult {
  const flat: WSEvent[] = [];
  const toolResultById = new Map<string, string>();
  const knownToolUseIds = new Set<string>();
  const agentToolUseIds = new Set<string>();
  const agentToolUseIdByAgentId = new Map<string, string>();
  const renderedTexts = new Set<string>();
  for (const ev of events) {
    const text = textBlockFromAgentMessage(ev);
    if (text && !text.startsWith("Codex native event_msg.agent_message\n\n```json\n")) {
      renderedTexts.add(text);
    }
    if (ev.type !== "agent_message") continue;
    const msg = (ev.data ?? {}) as { type?: string; message?: { content?: unknown } };
    if (msg.type !== "assistant") continue;
    const content = msg.message?.content;
    if (!Array.isArray(content)) continue;
    for (const raw of content) {
      if (!raw || typeof raw !== "object") continue;
      const block = raw as { type?: string; id?: string; name?: string };
      if ((block.type === "tool_use" || block.type === "server_tool_use") && typeof block.id === "string") {
        knownToolUseIds.add(block.id);
      }
      if (block.type === "tool_use" && block.name === "Agent" && typeof block.id === "string") {
        agentToolUseIds.add(block.id);
      }
    }
  }

  for (const ev of events) {
    if (ev.type !== "agent_message") {
      const _ts = deriveTs(ev);
      flat.push(_ts ? { ...ev, _ts } : ev);
      continue;
    }
    const msg = (ev.data ?? {}) as {
      type?: string;
      message?: { content?: unknown };
      parentUuid?: string | null;
      isSidechain?: boolean;
      parent_tool_use_id?: string | null;
    };
    const mtype = msg.type;
    if (
      mtype === "system" ||
      mtype === "queue-operation" ||
      mtype === "last-prompt" ||
      mtype === "attachment" ||
      mtype === "ai-title" ||
      mtype === "file-history-snapshot" ||
      mtype === "mode"
    ) {
      continue;
    }
    if (mtype === "lifecycle_notice") {
      const _ts = deriveTs(ev);
      flat.push({
        type: "lifecycle_notice",
        data: (msg as { data?: Record<string, unknown> }).data ?? {},
        _ts,
      });
      continue;
    }
    // Raw provider protocol envelopes that leaked through un-normalized
    // (Codex rollout line types). Never valid chat content — drop rather
    // than render as a diagnostic card.
    if (
      mtype === "response_item" ||
      mtype === "event_msg" ||
      mtype === "session_meta" ||
      mtype === "turn_context" ||
      mtype === "compacted" ||
      mtype === "thread.started"
    ) {
      continue;
    }
    const inner = msg.message;
    const content = inner && typeof inner === "object"
      ? (inner as { content?: unknown }).content
      : undefined;

    if (mtype === "user") {
      if (Array.isArray(content)) {
        for (const block of content) {
          if (!block || typeof block !== "object") continue;
          const b = block as {
            type?: string;
            tool_use_id?: string;
            content?: unknown;
          };
          if (b.type === "tool_result" && typeof b.tool_use_id === "string") {
            const resultText = toolResultContentToString(b.content);
            let toolUseId = b.tool_use_id;
            const mappedAgentToolUseId = agentToolUseIdByAgentId.get(toolUseId);
            if (mappedAgentToolUseId) {
              toolUseId = mappedAgentToolUseId;
            } else if (agentToolUseIds.has(toolUseId)) {
              const parsed = jsonObjectFromString(resultText);
              const agentId = parsed?.agent_id;
              if (typeof agentId === "string" && agentId) {
                agentToolUseIdByAgentId.set(agentId, toolUseId);
              }
            }
            toolResultById.set(
              toolUseId,
              resultText,
            );
            const paired = knownToolUseIds.has(toolUseId);
            const _ts = deriveTs(ev);
            flat.push({
              type: "tool_result",
              data: {
                output: resultText,
                tool_use_id: toolUseId,
                paired_tool_result: paired,
                orphan_tool_result: !paired,
              },
              _ts,
            });
          } else if (b.type !== "text" && b.type !== "image") {
            // Unknown user-side block (text is the prompt itself, image
            // renders as message attachments): surface as a diagnostic
            // card rather than silently dropping it.
            flat.push({
              type: "diagnostic",
              data: { kind: `user-block.${b.type || "(none)"}`, raw: block },
              _ts: deriveTs(ev),
            });
          }
        }
      }
      continue;
    }

    if (mtype !== "assistant") {
      const _ts = deriveTs(ev);
      flat.push({
        type: "diagnostic",
        data: { kind: `agent_message.${mtype || "(none)"}`, raw: msg },
        _ts,
      });
      continue;
    }
    if (!Array.isArray(content)) continue;

    const parent_tool_use_id: string | null =
      msg.parent_tool_use_id ?? null;

    const _ts = deriveTs(ev);
    for (const raw of content as unknown[]) {
      if (!raw || typeof raw !== "object") continue;
      const block = raw as {
        type?: string;
        text?: string;
        thinking?: string;
        name?: string;
        input?: unknown;
        id?: string;
      };
      const btype = block.type;
      if (btype === "text") {
        if (typeof block.text !== "string") continue;
        if (duplicateCodexNativeAgentMessage(block.text, renderedTexts)) continue;
        const notice = codexNativeFallbackLifecycleNotice(block.text);
        if (notice) {
          flat.push(_ts ? { ...notice, _ts } : notice);
          continue;
        }
        flat.push({
          type: "output",
          data: {
            output: block.text,
            parent_tool_use_id,
          },
          _ts,
        });
      } else if (btype === "thinking") {
        if (typeof block.thinking !== "string") continue;
        flat.push({
          type: "thinking",
          data: {
            thought: block.thinking,
            parent_tool_use_id,
          },
          _ts,
        });
      } else if (btype === "tool_use" || btype === "server_tool_use") {
        if (typeof block.name !== "string") continue;
        const toolName = normalizeToolName(block.name);
        flat.push({
          type: "tool_call",
          data: {
            tool: toolName,
            args: (block.input ?? null) as Record<string, unknown> | null,
            tool_use_id: block.id,
            parent_tool_use_id,
          },
          _ts,
        });
      } else if (btype === "tool_result") {
        const tr = raw as { tool_use_id?: string; content?: unknown };
        if (typeof tr.tool_use_id === "string") {
          const resultText = toolResultContentToString(tr.content);
          toolResultById.set(tr.tool_use_id, resultText);
          const paired = knownToolUseIds.has(tr.tool_use_id);
          flat.push({
            type: "tool_result",
            data: {
              output: resultText,
              tool_use_id: tr.tool_use_id,
              paired_tool_result: paired,
              orphan_tool_result: !paired,
            },
            _ts,
          });
        }
      } else if (btype === "fallback") {
        const fb = raw as { from?: { model?: unknown }; to?: { model?: unknown } };
        const fromModel = typeof fb.from?.model === "string" ? fb.from.model : "";
        const toModel = typeof fb.to?.model === "string" ? fb.to.model : "";
        flat.push({
          type: "model_fallback",
          data: { from_model: fromModel, to_model: toModel },
          _ts,
        });
      } else {
        flat.push({
          type: "diagnostic",
          data: { kind: `block.${btype || "(none)"}`, raw: block },
          _ts,
        });
      }
    }
  }

  return { flat, toolResultById };
}

function firstString(...values: unknown[]): string {
  for (const value of values) {
    if (typeof value === "string") return value;
  }
  return "";
}

export function extractAssistantTextFromEvents(
  events: WSEvent[],
  limit = 4000,
): string {
  const { flat } = flattenClaudeMessages(events);
  return flat
    .filter((event) => event.type === "output" || event.type === "thinking")
    .map((event) => {
      const data = event.data as Record<string, unknown> | undefined;
      if (event.type === "output") {
        return firstString(data?.output, data?.text, data?.content);
      }
      return firstString(data?.thought, data?.thinking, data?.text, data?.content);
    })
    .filter((text) => text.length > 0)
    .join("\n")
    .slice(0, limit);
}

export function extractAssistantOutputTextFromEvents(
  events: WSEvent[],
  limit = 4000,
): string {
  const { flat } = flattenClaudeMessages(events);
  return flat
    .filter((event) => event.type === "output")
    .map((event) => {
      const data = event.data as Record<string, unknown> | undefined;
      return firstString(data?.output, data?.text, data?.content);
    })
    .filter((text) => text.length > 0)
    .join("\n")
    .slice(0, limit);
}
