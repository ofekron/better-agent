export class SingleFlight<Key> {
  private readonly pending = new Map<Key, Promise<unknown>>();

  run<Value>(key: Key, work: () => Promise<Value>): Promise<Value> {
    const existing = this.pending.get(key) as Promise<Value> | undefined;
    if (existing) return existing;

    const promise = Promise.resolve().then(work);
    const tracked = promise.finally(() => {
      if (this.pending.get(key) === tracked) this.pending.delete(key);
    });
    this.pending.set(key, tracked);
    return tracked;
  }
}
