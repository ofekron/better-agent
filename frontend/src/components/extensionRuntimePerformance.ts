import { logDurable } from "src/lib/frontendLogger";

interface MountWindow {
  id: string;
  startedAt: number;
}

interface ScheduledMount {
  id: number;
  owner: string;
  priority: number;
  run: () => void;
}

const FRAME_BUDGET_MS = 8;
const MAX_MOUNTS_PER_FRAME = 1;
const scopes = new Map<string, ExtensionRuntimeScope>();

class ExtensionRuntimeScope {
  readonly key: string;
  private windows = new Map<number, MountWindow>();
  private observer: PerformanceObserver | null = null;
  private nextWindowId = 1;
  private queue: ScheduledMount[] = [];
  private nextJobId = 1;
  private frame = 0;

  constructor(key: string) {
    this.key = key;
  }

  beginWindow(id: string): () => void {
    const token = this.nextWindowId++;
    this.windows.set(token, { id, startedAt: performance.now() });
    this.ensureObserver();
    let finished = false;
    return () => {
      if (finished) return;
      finished = true;
      this.consume(this.observer?.takeRecords() ?? []);
      this.windows.delete(token);
      if (this.windows.size === 0) {
        this.observer?.disconnect();
        this.observer = null;
      }
    };
  }

  schedule(id: string, priority: number, run: () => void): () => void {
    const job: ScheduledMount = { id: this.nextJobId++, owner: id, priority, run };
    this.queue.push(job);
    this.queue.sort((left, right) => left.priority - right.priority || left.id - right.id);
    this.ensureFrame();
    return () => {
      this.queue = this.queue.filter((candidate) => candidate.id !== job.id);
      this.disposeIfIdle();
    };
  }

  dispose(): void {
    this.queue = [];
    if (this.frame) cancelAnimationFrame(this.frame);
    this.frame = 0;
    this.windows.clear();
    this.observer?.disconnect();
    this.observer = null;
  }

  isIdle(): boolean {
    return this.queue.length === 0 && this.windows.size === 0 && this.frame === 0;
  }

  private ensureObserver(): void {
    if (this.observer || typeof PerformanceObserver === "undefined") return;
    try {
      this.observer = new PerformanceObserver((list) => this.consume(list.getEntries()));
      this.observer.observe({ entryTypes: ["longtask"] });
    } catch {
      this.observer = null;
    }
  }

  private consume(entries: PerformanceEntry[]): void {
    for (const entry of entries) {
      const entryEnd = entry.startTime + entry.duration;
      const owners = [...this.windows.values()]
        .filter((window) => window.startedAt < entryEnd)
        .map((window) => window.id);
      if (owners.length === 0) continue;
      logDurable("extensions.module", "longtask", {
        scope: this.key,
        duration_ms: Math.round(entry.duration),
        owners,
      });
    }
  }

  private ensureFrame(): void {
    if (this.frame || this.queue.length === 0) return;
    this.frame = requestAnimationFrame(() => {
      this.frame = 0;
      const startedAt = performance.now();
      let ran = 0;
      while (
        this.queue.length > 0
        && ran < MAX_MOUNTS_PER_FRAME
        && (ran === 0 || performance.now() - startedAt < FRAME_BUDGET_MS)
      ) {
        const job = this.queue.shift();
        if (!job) break;
        job.run();
        ran += 1;
      }
      if (this.queue.length > 0) this.ensureFrame();
      this.disposeIfIdle();
    });
  }

  private disposeIfIdle(): void {
    if (!this.isIdle()) return;
    scopes.delete(this.key);
  }
}

function scopeFor(key: string): ExtensionRuntimeScope {
  let scope = scopes.get(key);
  if (!scope) {
    scope = new ExtensionRuntimeScope(key);
    scopes.set(key, scope);
  }
  return scope;
}

export function beginExtensionMountWindow(scopeKey: string, owner: string): () => void {
  return scopeFor(scopeKey).beginWindow(owner);
}

export function scheduleExtensionMount(
  scopeKey: string,
  owner: string,
  priority: number,
  run: () => void,
): () => void {
  return scopeFor(scopeKey).schedule(owner, priority, run);
}

export function disposeExtensionRuntime(scopeKey: string): void {
  const scope = scopes.get(scopeKey);
  scope?.dispose();
  scopes.delete(scopeKey);
}
