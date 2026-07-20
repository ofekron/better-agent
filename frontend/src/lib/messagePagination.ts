import type { ChatMessage, Session } from "../types";

export interface OlderMessagePage {
  messages: ChatMessage[];
  has_older: boolean;
  oldest_loaded_seq: number | null;
  total_messages: number;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

export function parseOlderMessagePage(value: unknown): OlderMessagePage {
  if (!isRecord(value) || !Array.isArray(value.messages)) {
    throw new Error("Invalid older-message page");
  }
  if (!value.messages.every((message) => isRecord(message) && typeof message.id === "string")) {
    throw new Error("Invalid older-message page messages");
  }
  const ids = new Set<string>();
  const sequences: number[] = [];
  for (const message of value.messages) {
    const record = message as Record<string, unknown>;
    if (ids.has(record.id as string)) {
      throw new Error("Older-message page contains duplicate message IDs");
    }
    ids.add(record.id as string);
    if (!Number.isInteger(record.seq)) {
      throw new Error("Older-message page messages require integer sequences");
    }
    sequences.push(record.seq as number);
  }
  for (let index = 1; index < sequences.length; index += 1) {
    if (sequences[index] <= sequences[index - 1]) {
      throw new Error("Older-message page messages must be strictly ordered");
    }
  }
  if (typeof value.has_older !== "boolean") {
    throw new Error("Invalid older-message page has_older");
  }
  if (!Number.isInteger(value.total_messages) || (value.total_messages as number) < 0) {
    throw new Error("Invalid older-message page total_messages");
  }
  if (value.oldest_loaded_seq !== null && !Number.isInteger(value.oldest_loaded_seq)) {
    throw new Error("Invalid older-message page oldest_loaded_seq");
  }
  if (value.messages.length === 0 && value.oldest_loaded_seq !== null) {
    throw new Error("Empty older-message page must have a null oldest_loaded_seq");
  }
  if (value.messages.length === 0 && value.has_older) {
    throw new Error("Empty older-message page cannot have older messages");
  }
  if (value.messages.length > 0 && typeof value.oldest_loaded_seq !== "number") {
    throw new Error("Non-empty older-message page must have a numeric oldest_loaded_seq");
  }
  if (sequences.length > 0 && value.oldest_loaded_seq !== sequences[0]) {
    throw new Error("Older-message page oldest_loaded_seq must match its first message");
  }
  return value as unknown as OlderMessagePage;
}

function loadedBoundary(node: Session): number | null {
  const paginationBoundary = node.pagination?.oldest_loaded_seq;
  return typeof paginationBoundary === "number" ? paginationBoundary : null;
}

export function applyOlderMessagePage(
  node: Session,
  beforeSeq: number,
  page: OlderMessagePage,
): Session {
  const boundary = loadedBoundary(node);
  if (boundary === null || beforeSeq !== boundary) return node;
  if (page.messages.some((message) => (message.seq as number) >= beforeSeq)) {
    throw new Error("Older-message page did not advance the loaded boundary");
  }

  const existing = node.messages ?? [];
  const seen = new Set(existing.map((message) => message.id));
  const unseenOlder = page.messages.filter((message) => {
    if (seen.has(message.id)) return false;
    seen.add(message.id);
    return true;
  });
  return {
    ...node,
    messages: unseenOlder.length > 0 ? [...unseenOlder, ...existing] : existing,
    pagination: {
      total_messages: page.total_messages,
      oldest_loaded_seq: page.oldest_loaded_seq,
      has_older: page.has_older,
    },
  };
}
