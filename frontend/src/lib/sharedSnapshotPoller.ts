export interface SnapshotPollOptions<T> {
  load: () => Promise<T>;
  minIntervalMs: number;
  cadenceMs?: number;
  maxBackoffMs?: number;
  now?: () => number;
  random?: () => number;
  idleTtlMs?: number;
}

export interface AuthorityEnvelope {
  authority_epoch: string;
  revision: number;
}

export interface SnapshotPollMetrics {
  attempts: number;
  suppressed: number;
  coalesced: number;
  backoffs: number;
}

type Listener<T> = (value: T) => void;

export class SharedSnapshotPoller<T> {
  private options: SnapshotPollOptions<T>;
  private value: T | undefined;
  private inFlight: Promise<boolean> | null = null;
  private nextAttemptAt = 0;
  private failures = 0;
  private timer: number | undefined;
  private disposeTimer: number | undefined;
  private authority: AuthorityEnvelope | null = null;
  private readonly retiredEpochs = new Set<string>();
  private readonly listeners = new Set<Listener<T>>();
  readonly metrics: SnapshotPollMetrics = { attempts: 0, suppressed: 0, coalesced: 0, backoffs: 0 };

  constructor(options: SnapshotPollOptions<T>) { this.options = options; }

  update(options: SnapshotPollOptions<T>): void {
    this.options = options;
  }

  current(): T | undefined { return this.value; }

  subscribe(listener: Listener<T>): () => void {
    if (this.disposeTimer !== undefined) window.clearTimeout(this.disposeTimer);
    this.disposeTimer = undefined;
    this.listeners.add(listener);
    if (this.value !== undefined) listener(this.value);
    if (this.listeners.size === 1) this.bind();
    void this.request();
    return () => {
      this.listeners.delete(listener);
      if (this.listeners.size === 0) {
        this.unbind();
        this.disposeTimer = window.setTimeout(() => this.dispose(), this.options.idleTtlMs ?? 60_000);
      }
    };
  }

  request(): Promise<boolean> {
    if (typeof document !== "undefined" && document.hidden) {
      this.metrics.suppressed += 1;
      return Promise.resolve(false);
    }
    if (this.inFlight) {
      this.metrics.coalesced += 1;
      return this.inFlight;
    }
    const now = this.options.now?.() ?? Date.now();
    if (now < this.nextAttemptAt) {
      this.metrics.suppressed += 1;
      return Promise.resolve(false);
    }
    this.nextAttemptAt = now + this.options.minIntervalMs;
    this.metrics.attempts += 1;
    const startedAuthority = this.authority;
    this.inFlight = this.options.load().then((value) => {
      if (startedAuthority !== this.authority && !this.acceptAuthority(value)) return false;
      if (startedAuthority === this.authority && !this.acceptAuthority(value)) return false;
      this.value = value;
      this.failures = 0;
      for (const listener of this.listeners) listener(value);
      return true;
    }).catch(() => {
      this.failures += 1;
      this.metrics.backoffs += 1;
      const max = this.options.maxBackoffMs ?? 5 * 60_000;
      const base = Math.min(max, this.options.minIntervalMs * 2 ** Math.max(0, this.failures - 1));
      const random = this.options.random?.() ?? Math.random();
      this.nextAttemptAt = now + Math.round(base * (0.75 + random * 0.5));
      return false;
    }).finally(() => { this.inFlight = null; });
    return this.inFlight;
  }

  publish(value: T): void {
    if (!this.acceptAuthority(value)) return;
    this.value = value;
    for (const listener of this.listeners) listener(value);
  }

  dispose(): void {
    this.unbind();
    if (this.disposeTimer !== undefined) window.clearTimeout(this.disposeTimer);
    this.disposeTimer = undefined;
    this.listeners.clear();
    this.onDispose?.();
  }

  onDispose?: () => void;

  private acceptAuthority(value: T): boolean {
    const envelope = value as Partial<AuthorityEnvelope> | null;
    if (!envelope || typeof envelope.authority_epoch !== "string"
      || typeof envelope.revision !== "number" || !Number.isInteger(envelope.revision)) return true;
    const next = envelope as AuthorityEnvelope;
    if (this.retiredEpochs.has(next.authority_epoch)) return false;
    if (!this.authority) { this.authority = next; return true; }
    if (next.authority_epoch !== this.authority.authority_epoch) {
      this.retiredEpochs.add(this.authority.authority_epoch);
      this.authority = next;
      return true;
    }
    if (next.revision < this.authority.revision) return false;
    this.authority = next;
    return true;
  }

  private readonly onResume = () => { void this.request(); };

  private bind(): void {
    document.addEventListener("visibilitychange", this.onResume);
    window.addEventListener("focus", this.onResume);
    window.addEventListener("online", this.onResume);
    if (this.options.cadenceMs) this.timer = window.setInterval(this.onResume, this.options.cadenceMs);
  }

  private unbind(): void {
    document.removeEventListener("visibilitychange", this.onResume);
    window.removeEventListener("focus", this.onResume);
    window.removeEventListener("online", this.onResume);
    if (this.timer !== undefined) window.clearInterval(this.timer);
    this.timer = undefined;
  }
}

const pollers = new Map<string, SharedSnapshotPoller<unknown>>();

export function sharedSnapshotPoller<T>(key: string, options: SnapshotPollOptions<T>): SharedSnapshotPoller<T> {
  const existing = pollers.get(key) as SharedSnapshotPoller<T> | undefined;
  if (existing) {
    existing.update(options);
    return existing;
  }
  const created = new SharedSnapshotPoller(options);
  created.onDispose = () => { if (pollers.get(key) === created) pollers.delete(key); };
  pollers.set(key, created as SharedSnapshotPoller<unknown>);
  return created;
}

export function scopedSnapshotKey(api: string, authScopeKey: string, domain: string): string {
  return JSON.stringify([api, authScopeKey, domain]);
}

export function resetSharedSnapshotPollersForTest(): void {
  for (const poller of pollers.values()) poller.dispose();
  pollers.clear();
}
