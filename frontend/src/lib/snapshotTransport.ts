import type { WSEvent } from "../types";

const MAX_SNAPSHOT_BYTES = 16 * 1024 * 1024;
const MAX_STAGED_BYTES = 32 * 1024 * 1024;
const MAX_ACTIVE_TRANSFERS = 8;
const MAX_BUFFERED_LIVE_BYTES = 4 * 1024 * 1024;
const MAX_BUFFERED_LIVE_EVENTS = 2048;
const MAX_CHUNKS = 128;
const MAX_CHUNK_BYTES = 180 * 1024;
const SHA256_HEX = /^[0-9a-f]{64}$/;
const REVISION = /^[A-Za-z0-9:_-]{1,80}$/;
const BASE64 = /^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/;
const SNAPSHOT_EVENT_TYPES = new Set(["messages_replay", "stub_invalidated", "rewind_complete"]);
const UTF8_ENCODER = new TextEncoder();

type SendFrame = (frame: Record<string, unknown>) => void;
type ApplyEvent = (event: WSEvent) => void | Promise<void>;
type RefreshReason = "restart_required" | "corrupt" | "overflow" | "too_large";

type Transfer = {
  snapshotId: string;
  key: string;
  eventType: string;
  revision: string;
  refreshId: string;
  digest: string;
  totalBytes: number;
  totalChunks: number;
  chunkBytes: number;
  chunks: Map<number, Uint8Array>;
  receivedBytes: number;
  nextChunk: number;
  generation: number;
  state: "pending" | "ready" | "cancelled";
  readyEvent?: WSEvent;
};

type OrderedItem =
  | { kind: "snapshot"; transfer: Transfer }
  | { kind: "live"; event: WSEvent; bytes: number; rootId: string | null };

function objectData(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function integer(value: unknown, min: number, max: number): number | null {
  return Number.isInteger(value) && Number(value) >= min && Number(value) <= max
    ? Number(value)
    : null;
}

function decodeBase64(value: unknown): Uint8Array | null {
  if (typeof value !== "string" || value.length === 0 || !BASE64.test(value)) return null;
  try {
    const decoded = atob(value);
    if (decoded.length > MAX_CHUNK_BYTES) return null;
    return Uint8Array.from(decoded, (char) => char.charCodeAt(0));
  } catch {
    return null;
  }
}

async function sha256Hex(bytes: Uint8Array<ArrayBuffer>): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

function yieldToBrowser(): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, 0));
}

function validSnapshotEvent(value: unknown, eventType: string): value is WSEvent {
  const event = objectData(value);
  if (!event || event.type !== eventType) return false;
  if (eventType === "rewind_complete") {
    const data = objectData(event.data);
    return typeof data?.session_id === "string" && Array.isArray(data.messages);
  }
  return objectData(event.data) !== null;
}

function eventRootId(event: WSEvent): string | null {
  const data = objectData(event.data);
  for (const value of [data?.root_id, data?.app_session_id, data?.session_id]) {
    if (typeof value === "string" && value) return value;
  }
  return null;
}

function snapshotKeyRoot(key: string): string | null {
  const separator = key.indexOf(":");
  if (separator < 0 || separator === key.length - 1) return null;
  const rootId = key.slice(separator + 1);
  return rootId === "global" ? null : rootId;
}

function compareCodePoints(left: string, right: string): number {
  const a = Array.from(left);
  const b = Array.from(right);
  for (let index = 0; index < Math.min(a.length, b.length); index += 1) {
    const difference = a[index].codePointAt(0)! - b[index].codePointAt(0)!;
    if (difference !== 0) return difference;
  }
  return a.length - b.length;
}

function authorityScope(data: Record<string, unknown>, rootId: string): string[] | null {
  const values = data.scope_sids;
  if (!Array.isArray(values) || values.length < 1 || values.length > 512) return null;
  let totalBytes = 0;
  let previous: string | null = null;
  const scope: string[] = [];
  for (const value of values) {
    if (typeof value !== "string" || value.length === 0 || Array.from(value).length > 256) return null;
    if (previous !== null && compareCodePoints(previous, value) >= 0) return null;
    totalBytes += UTF8_ENCODER.encode(value).byteLength;
    if (totalBytes > 128 * 1024) return null;
    scope.push(value);
    previous = value;
  }
  return scope.includes(rootId) ? scope : null;
}

export class SnapshotTransport {
  private readonly byKey = new Map<string, Transfer>();
  private readonly keyById = new Map<string, string>();
  private readonly ordered: OrderedItem[] = [];
  private bufferedLiveBytes = 0;
  private bufferedLiveEvents = 0;
  private generation = 0;
  private readonly supersededKeys = new Set<string>();
  private readonly recoveries = new Map<string, {
    terminal: boolean;
    pending: Set<Promise<void>>;
    key: string;
    eventType: string;
    revision: string;
    authorityRoots: Set<string>;
    terminalRoots: Set<string> | null;
  }>();

  handle(
    event: { type?: unknown; data?: unknown },
    send: SendFrame,
    apply: ApplyEvent,
    wireBytes = 0,
  ): boolean {
    if (event.type === "snapshot_begin") {
      this.begin(event.data, send, apply);
      return true;
    }
    if (event.type === "snapshot_chunk") {
      this.chunk(event.data, send, apply);
      return true;
    }
    if (event.type === "snapshot_end") {
      void this.end(event.data, send, apply);
      return true;
    }
    if (event.type === "snapshot_restart_required") {
      this.restartRequired(event.data, send, apply);
      return true;
    }
    if (event.type === "snapshot_refresh_required") {
      this.refreshRequired(event.data, send, apply);
      return true;
    }
    if (event.type === "snapshot_cancelled") {
      this.cancelled(event.data, apply);
      return true;
    }
    if (event.type === "snapshot_refresh_complete") {
      this.refreshComplete(event.data, apply);
      return true;
    }
    if (this.recoveries.size > 0 && event.type === "session_reconciled") {
      this.reconcileAuthority(event as WSEvent, apply);
      return true;
    }
    if (this.recoveries.size > 0) {
      this.bufferRecoveryLive(event as WSEvent, wireBytes, send);
      return true;
    }
    if (this.supersededKeys.size > 0) return true;
    if (this.ordered.length > 0) {
      this.bufferLive(event as WSEvent, wireBytes, send, apply);
      return true;
    }
    return false;
  }

  resume(send: SendFrame): void {
    for (const transfer of this.byKey.values()) {
      send({
        type: "snapshot_resume",
        data: {
          snapshot_id: transfer.snapshotId,
          revision: transfer.revision,
          digest: transfer.digest,
          next_chunk: transfer.nextChunk,
        },
      });
    }
  }

  clear(): void {
    for (const transfer of [...this.byKey.values()]) this.discard(transfer);
    this.ordered.length = 0;
    this.bufferedLiveBytes = 0;
    this.bufferedLiveEvents = 0;
    this.recoveries.clear();
    this.supersededKeys.clear();
  }

  private begin(raw: unknown, send: SendFrame, apply: ApplyEvent): void {
    const data = objectData(raw);
    if (!data) return;
    const snapshotId = typeof data.snapshot_id === "string" ? data.snapshot_id : "";
    const key = typeof data.key === "string" ? data.key : "";
    const eventType = typeof data.event_type === "string" ? data.event_type : "";
    const revision = typeof data.revision === "string" ? data.revision : "";
    const digest = typeof data.digest === "string" ? data.digest : "";
    const refreshId = typeof data.refresh_id === "string" ? data.refresh_id : "";
    const rawTotalBytes = Number.isInteger(data.total_bytes) ? Number(data.total_bytes) : null;
    const totalBytes = integer(data.total_bytes, 1, MAX_SNAPSHOT_BYTES);
    const totalChunks = integer(data.total_chunks, 1, MAX_CHUNKS);
    const chunkBytes = integer(data.chunk_bytes, 1, MAX_CHUNK_BYTES);
    const resumeFrom = integer(data.resume_from, 0, totalChunks ?? 0);
    if (key.length > 300 || !/^[0-9a-f]{32}$/.test(refreshId)) return;
    if (rawTotalBytes !== null && rawTotalBytes > MAX_SNAPSHOT_BYTES
      && SNAPSHOT_EVENT_TYPES.has(eventType) && REVISION.test(revision)) {
      this.requestRecovery(send, "too_large", { key, eventType, revision, refreshId });
      return;
    }
    if (!snapshotId || !key || !SNAPSHOT_EVENT_TYPES.has(eventType) || !REVISION.test(revision)
      || !SHA256_HEX.test(digest) || totalBytes === null || totalChunks === null
      || chunkBytes === null || resumeFrom === null
      || totalChunks !== Math.ceil(totalBytes / chunkBytes)) return;
    this.supersededKeys.delete(key);

    const current = this.byKey.get(key);
    if (resumeFrom > 0 && current?.snapshotId === snapshotId
      && current.revision === revision && current.nextChunk === resumeFrom
      && current.totalBytes === totalBytes && current.totalChunks === totalChunks
      && current.chunkBytes === chunkBytes) {
      this.ack(current, send);
      return;
    }
    if (resumeFrom > 0) return;

    this.supersedeKey(key, send);
    const stagedBytes = [...this.byKey.values()].reduce((total, transfer) => total + transfer.totalBytes, 0);
    if (this.byKey.size >= MAX_ACTIVE_TRANSFERS || stagedBytes + totalBytes > MAX_STAGED_BYTES) {
      this.failBoundary(send, apply, "overflow", { key, eventType, revision, refreshId });
      return;
    }
    const transfer: Transfer = {
      snapshotId, key, eventType, revision, refreshId, digest, totalBytes, totalChunks,
      chunkBytes, chunks: new Map(), receivedBytes: 0, nextChunk: 0,
      generation: ++this.generation, state: "pending",
    };
    this.byKey.set(key, transfer);
    this.keyById.set(snapshotId, key);
    this.ordered.push({ kind: "snapshot", transfer });
    this.ack(transfer, send);
  }

  private chunk(raw: unknown, send: SendFrame, apply: ApplyEvent): void {
    const data = objectData(raw);
    if (!data) return;
    const snapshotId = typeof data.snapshot_id === "string" ? data.snapshot_id : "";
    const key = this.keyById.get(snapshotId);
    const transfer = key ? this.byKey.get(key) : undefined;
    if (!transfer || data.revision !== transfer.revision) return;
    const index = integer(data.index, 0, transfer.totalChunks - 1);
    const bytes = decodeBase64(data.payload);
    if (index === null || !bytes) {
      this.failBoundary(send, apply, "corrupt", transfer);
      return;
    }
    const expectedBytes = index === transfer.totalChunks - 1
      ? transfer.totalBytes - (transfer.totalChunks - 1) * transfer.chunkBytes
      : transfer.chunkBytes;
    if (bytes.length !== expectedBytes) {
      this.failBoundary(send, apply, "corrupt", transfer);
      return;
    }
    const prior = transfer.chunks.get(index);
    if (prior) {
      if (prior.length !== bytes.length || prior.some((byte, offset) => byte !== bytes[offset])) {
        this.failBoundary(send, apply, "corrupt", transfer);
      } else {
        this.ack(transfer, send);
      }
      return;
    }
    if (transfer.receivedBytes + bytes.length > transfer.totalBytes) {
      this.failBoundary(send, apply, "corrupt", transfer);
      return;
    }
    transfer.chunks.set(index, bytes);
    transfer.receivedBytes += bytes.length;
    while (transfer.chunks.has(transfer.nextChunk)) transfer.nextChunk += 1;
    this.ack(transfer, send);
  }

  private async end(raw: unknown, send: SendFrame, apply: ApplyEvent): Promise<void> {
    const data = objectData(raw);
    if (!data) return;
    const snapshotId = typeof data.snapshot_id === "string" ? data.snapshot_id : "";
    const key = this.keyById.get(snapshotId);
    const transfer = key ? this.byKey.get(key) : undefined;
    if (!transfer || data.revision !== transfer.revision || data.digest !== transfer.digest
      || data.total_bytes !== transfer.totalBytes || data.total_chunks !== transfer.totalChunks) return;
    if (transfer.nextChunk !== transfer.totalChunks || transfer.receivedBytes !== transfer.totalBytes) {
      this.ack(transfer, send);
      return;
    }
    const generation = transfer.generation;
    await yieldToBrowser();
    if (!this.isCurrent(transfer, generation)) return;
    const bytes = new Uint8Array(transfer.totalBytes);
    let offset = 0;
    for (let index = 0; index < transfer.totalChunks; index += 1) {
      const chunk = transfer.chunks.get(index);
      if (!chunk) return;
      bytes.set(chunk, offset);
      offset += chunk.length;
    }
    let digest: string;
    try {
      digest = await sha256Hex(bytes);
    } catch {
      this.failBoundary(send, apply, "corrupt", transfer);
      return;
    }
    if (!this.isCurrent(transfer, generation)) return;
    if (digest !== transfer.digest) {
      this.failBoundary(send, apply, "corrupt", transfer);
      return;
    }
    await yieldToBrowser();
    if (!this.isCurrent(transfer, generation)) return;
    try {
      const parsed: unknown = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
      if (!validSnapshotEvent(parsed, transfer.eventType)) {
        this.failBoundary(send, apply, "corrupt", transfer);
        return;
      }
      this.detach(transfer);
      transfer.readyEvent = parsed;
      transfer.state = "ready";
      this.drain(apply);
    } catch {
      this.failBoundary(send, apply, "corrupt", transfer);
    }
  }

  private restartRequired(raw: unknown, send: SendFrame, apply: ApplyEvent): void {
    const data = objectData(raw);
    const snapshotId = typeof data?.snapshot_id === "string" ? data.snapshot_id : "";
    const key = this.keyById.get(snapshotId);
    const transfer = key ? this.byKey.get(key) : undefined;
    if (transfer) this.failBoundary(send, apply, "restart_required", transfer);
  }

  private refreshRequired(raw: unknown, send: SendFrame, _apply: ApplyEvent): void {
    const data = objectData(raw);
    const key = typeof data?.key === "string" ? data.key : "";
    const eventType = typeof data?.event_type === "string" ? data.event_type : "";
    const revision = typeof data?.revision === "string" ? data.revision : "";
    const refreshId = typeof data?.refresh_id === "string" ? data.refresh_id : "";
    const reason = data?.reason === "overflow" || data?.reason === "too_large"
      ? data.reason
      : null;
    if (!key || key.length > 300 || !SNAPSHOT_EVENT_TYPES.has(eventType)
      || !REVISION.test(revision) || !/^[0-9a-f]{32}$/.test(refreshId) || !reason) return;
    this.requestRecovery(send, reason, { key, eventType, revision, refreshId });
  }

  private cancelled(raw: unknown, _apply: ApplyEvent): void {
    const data = objectData(raw);
    const snapshotId = typeof data?.snapshot_id === "string" ? data.snapshot_id : "";
    const key = this.keyById.get(snapshotId);
    const transfer = key ? this.byKey.get(key) : undefined;
    if (!transfer) return;
    this.supersededKeys.add(transfer.key);
    this.discard(transfer);
    this.discardBufferedDependencies();
  }

  private ack(transfer: Transfer, send: SendFrame): void {
    send({
      type: "snapshot_ack",
      data: {
        snapshot_id: transfer.snapshotId,
        revision: transfer.revision,
        next_chunk: transfer.nextChunk,
      },
    });
  }

  private isCurrent(transfer: Transfer, generation: number): boolean {
    return transfer.generation === generation && this.byKey.get(transfer.key) === transfer;
  }

  private discard(transfer: Transfer): void {
    transfer.state = "cancelled";
    this.detach(transfer);
  }

  private detach(transfer: Transfer): void {
    transfer.generation = ++this.generation;
    this.byKey.delete(transfer.key);
    this.keyById.delete(transfer.snapshotId);
    transfer.chunks.clear();
  }

  private bufferLive(event: WSEvent, wireBytes: number, send: SendFrame, apply: ApplyEvent): void {
    const bytes = Math.max(0, wireBytes);
    if (this.bufferedLiveEvents >= MAX_BUFFERED_LIVE_EVENTS
      || this.bufferedLiveBytes + bytes > MAX_BUFFERED_LIVE_BYTES) {
      this.failBoundary(send, apply, "overflow");
      return;
    }
    this.ordered.push({ kind: "live", event, bytes, rootId: eventRootId(event) });
    this.bufferedLiveBytes += bytes;
    this.bufferedLiveEvents += 1;
  }

  private drain(apply: ApplyEvent): void {
    while (this.ordered.length > 0) {
      const item = this.ordered[0];
      if (item.kind === "snapshot") {
        if (item.transfer.state === "pending") return;
        this.ordered.shift();
        if (item.transfer.state === "ready" && item.transfer.readyEvent) {
          apply(item.transfer.readyEvent);
        }
        continue;
      }
      this.ordered.shift();
      this.bufferedLiveBytes -= item.bytes;
      this.bufferedLiveEvents -= 1;
      apply(item.event);
    }
  }

  private failBoundary(
    send: SendFrame,
    _apply: ApplyEvent,
    reason: RefreshReason,
    trigger?: Transfer | { key: string; eventType: string; revision: string; refreshId: string },
  ): void {
    const refreshes = new Map<string, {
      key: string; eventType: string; revision: string; refreshId: string;
    }>();
    for (const transfer of this.byKey.values()) {
      refreshes.set(transfer.key, transfer);
    }
    if (trigger) refreshes.set(trigger.key, trigger);
    const existingRecoveries = [...this.recoveries.entries()];
    this.clear();
    for (const [refreshId, recovery] of existingRecoveries) {
      this.recoveries.set(refreshId, recovery);
    }
    for (const transfer of refreshes.values()) {
      send({
        type: "snapshot_refresh",
        data: {
          key: transfer.key,
          event_type: transfer.eventType,
          failed_revision: transfer.revision,
          refresh_id: transfer.refreshId,
          reason,
        },
      });
      this.recoveries.set(transfer.refreshId, {
        terminal: false, pending: new Set(), key: transfer.key,
        eventType: transfer.eventType, revision: transfer.revision,
        authorityRoots: new Set(), terminalRoots: null,
      });
    }
  }

  private requestRecovery(
    send: SendFrame,
    reason: RefreshReason,
    transfer: { key: string; eventType: string; revision: string; refreshId: string },
  ): void {
    const rootId = snapshotKeyRoot(transfer.key);
    if (rootId) this.discardBufferedLiveForRoot(rootId);
    send({
      type: "snapshot_refresh",
      data: {
        key: transfer.key,
        event_type: transfer.eventType,
        failed_revision: transfer.revision,
        refresh_id: transfer.refreshId,
        reason,
      },
    });
    this.recoveries.set(transfer.refreshId, {
      terminal: false,
      pending: new Set(),
      key: transfer.key,
      eventType: transfer.eventType,
      revision: transfer.revision,
      authorityRoots: new Set(),
      terminalRoots: null,
    });
  }

  private discardBufferedDependencies(): void {
    for (const transfer of [...this.byKey.values()]) this.discard(transfer);
    this.ordered.length = 0;
    this.bufferedLiveBytes = 0;
    this.bufferedLiveEvents = 0;
  }

  private supersedeKey(key: string, send: SendFrame): void {
    if (!this.byKey.has(key)) return;
    const unrelated = [...this.byKey.values()].filter((transfer) => transfer.key !== key);
    this.clear();
    for (const transfer of unrelated) {
      send({
        type: "snapshot_refresh",
        data: {
          key: transfer.key,
          event_type: transfer.eventType,
          failed_revision: transfer.revision,
          refresh_id: transfer.refreshId,
          reason: "restart_required",
        },
      });
      this.recoveries.set(transfer.refreshId, {
        terminal: false, pending: new Set(), key: transfer.key,
        eventType: transfer.eventType, revision: transfer.revision,
        authorityRoots: new Set(), terminalRoots: null,
      });
    }
  }

  private reconcileAuthority(event: WSEvent, apply: ApplyEvent): void {
    const data = objectData(event.data);
    const refreshId = typeof data?.snapshot_refresh_id === "string" ? data.snapshot_refresh_id : "";
    const rootId = typeof data?.root_id === "string" ? data.root_id : "";
    const recovery = this.recoveries.get(refreshId);
    if (!recovery || !rootId || !data) return;
    const scope = authorityScope(data, rootId);
    if (!scope) return;
    for (const sessionId of scope) this.discardBufferedLiveForRoot(sessionId);
    recovery.authorityRoots.add(rootId);
    const result = apply(event);
    if (!result || typeof (result as Promise<void>).then !== "function") return;
    const pending = Promise.resolve(result);
    recovery.pending.add(pending);
    void pending.then(
      () => {
        recovery.pending.delete(pending);
        this.finishRecoveryIfReady(apply);
      },
      () => {
        recovery.pending.delete(pending);
      },
    );
  }

  private refreshComplete(raw: unknown, apply: ApplyEvent): void {
    const data = objectData(raw);
    const refreshId = typeof data?.refresh_id === "string" ? data.refresh_id : "";
    const recovery = this.recoveries.get(refreshId);
    if (!recovery || data?.success !== true || !Array.isArray(data.root_ids)
      || !data.root_ids.every((rootId) => typeof rootId === "string" && rootId)) return;
    recovery.terminal = true;
    recovery.terminalRoots = new Set(data.root_ids as string[]);
    this.finishRecoveryIfReady(apply);
  }

  private finishRecoveryIfReady(apply: ApplyEvent): void {
    for (const recovery of this.recoveries.values()) {
      if (!recovery.terminal || recovery.pending.size > 0 || !recovery.terminalRoots) return;
      for (const rootId of recovery.terminalRoots) {
        if (!recovery.authorityRoots.has(rootId)) return;
      }
    }
    this.recoveries.clear();
    this.drain(apply);
  }

  private bufferRecoveryLive(event: WSEvent, wireBytes: number, send: SendFrame): void {
    const bytes = Math.max(0, wireBytes);
    if (this.bufferedLiveEvents >= MAX_BUFFERED_LIVE_EVENTS
      || this.bufferedLiveBytes + bytes > MAX_BUFFERED_LIVE_BYTES) {
      this.discardBufferedLiveOnly();
      for (const [refreshId, recovery] of this.recoveries) {
        send({ type: "snapshot_refresh", data: {
          key: recovery.key,
          event_type: recovery.eventType,
          failed_revision: recovery.revision,
          refresh_id: refreshId,
          reason: "overflow",
        } });
      }
      return;
    }
    this.ordered.push({ kind: "live", event, bytes, rootId: eventRootId(event) });
    this.bufferedLiveBytes += bytes;
    this.bufferedLiveEvents += 1;
  }

  private discardBufferedLiveOnly(): void {
    for (let index = this.ordered.length - 1; index >= 0; index -= 1) {
      if (this.ordered[index].kind === "live") this.ordered.splice(index, 1);
    }
    this.bufferedLiveBytes = 0;
    this.bufferedLiveEvents = 0;
  }

  private discardBufferedLiveForRoot(rootId: string): void {
    for (let index = this.ordered.length - 1; index >= 0; index -= 1) {
      const item = this.ordered[index];
      if (item.kind !== "live" || item.rootId !== rootId) continue;
      this.bufferedLiveBytes -= item.bytes;
      this.bufferedLiveEvents -= 1;
      this.ordered.splice(index, 1);
    }
  }
}
