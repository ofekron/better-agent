import type { OfflineQueueEntry } from "src/hooks/useOfflineQueue";

const DB_NAME = "better-agent-offline-actions";
const DB_VERSION = 1;
const ACTIONS = "actions";
const PAYLOADS = "payloads";
const SEQUENCE = "sequence";

type StoredAction = Omit<OfflineQueueEntry, "images" | "files"> & {
  key: string;
  order: number;
};

type StoredPayload = Pick<OfflineQueueEntry, "images" | "files"> & { key: string };

export function offlineActionKey(entry: OfflineQueueEntry): string {
  const sessionId = entry.type === "create_session" ? entry.session.id : entry.sessionId;
  return offlineActionKeyFor(sessionId, entry.clientId);
}

export function offlineActionKeyFor(sessionId: string, clientId: string): string {
  return `${sessionId}\u0000${clientId}`;
}

function requestResult<T>(request: IDBRequest<T>): Promise<T> {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error ?? new Error("IndexedDB request failed"));
  });
}

function transactionDone(transaction: IDBTransaction): Promise<void> {
  return new Promise((resolve, reject) => {
    transaction.oncomplete = () => resolve();
    transaction.onerror = () => reject(transaction.error ?? new Error("IndexedDB transaction failed"));
    transaction.onabort = () => reject(transaction.error ?? new Error("IndexedDB transaction aborted"));
  });
}

function openDatabase(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, DB_VERSION);
    request.onupgradeneeded = () => {
      const db = request.result;
      if (!db.objectStoreNames.contains(ACTIONS)) db.createObjectStore(ACTIONS, { keyPath: "key" });
      if (!db.objectStoreNames.contains(PAYLOADS)) db.createObjectStore(PAYLOADS, { keyPath: "key" });
      if (!db.objectStoreNames.contains(SEQUENCE)) db.createObjectStore(SEQUENCE, { autoIncrement: true });
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error ?? new Error("IndexedDB open failed"));
  });
}

function splitEntry(entry: OfflineQueueEntry, key: string, order: number): {
  action: StoredAction;
  payload: StoredPayload | null;
} {
  const { images, files, ...metadata } = entry;
  return {
    action: { ...metadata, key, order } as StoredAction,
    payload: images?.length || files?.length ? { key, images, files } : null,
  };
}

function hydrate(action: StoredAction, payload?: StoredPayload): OfflineQueueEntry {
  const { key: _key, order: _order, ...entry } = action;
  void _key;
  void _order;
  return {
    ...entry,
    ...(payload?.images?.length ? { images: payload.images } : {}),
    ...(payload?.files?.length ? { files: payload.files } : {}),
  } as OfflineQueueEntry;
}

export async function loadOfflineActions(): Promise<OfflineQueueEntry[]> {
  const db = await openDatabase();
  const tx = db.transaction([ACTIONS, PAYLOADS], "readonly");
  const [actions, payloads] = await Promise.all([
    requestResult(tx.objectStore(ACTIONS).getAll()) as Promise<StoredAction[]>,
    requestResult(tx.objectStore(PAYLOADS).getAll()) as Promise<StoredPayload[]>,
  ]);
  await transactionDone(tx);
  db.close();
  const payloadByKey = new Map(payloads.map((payload) => [payload.key, payload]));
  return actions
    .sort((left, right) => left.order - right.order)
    .map((action) => hydrate(action, payloadByKey.get(action.key)));
}

export async function putOfflineAction(entry: OfflineQueueEntry): Promise<void> {
  const db = await openDatabase();
  const tx = db.transaction([ACTIONS, PAYLOADS, SEQUENCE], "readwrite");
  const actions = tx.objectStore(ACTIONS);
  const payloads = tx.objectStore(PAYLOADS);
  const key = offlineActionKey(entry);
  const existing = await requestResult(actions.get(key)) as StoredAction | undefined;
  const order = existing?.order ?? Number(await requestResult(tx.objectStore(SEQUENCE).add({})));
  const { action, payload } = splitEntry(entry, key, order);
  actions.put(action);
  if (payload) payloads.put(payload);
  else payloads.delete(key);
  await transactionDone(tx);
  db.close();
}

export async function importOfflineActions(entries: OfflineQueueEntry[]): Promise<void> {
  const db = await openDatabase();
  const tx = db.transaction([ACTIONS, PAYLOADS, SEQUENCE], "readwrite");
  const done = transactionDone(tx);
  const actions = tx.objectStore(ACTIONS);
  const payloads = tx.objectStore(PAYLOADS);
  const sequence = tx.objectStore(SEQUENCE);
  try {
    for (const entry of entries) {
      const key = offlineActionKey(entry);
      if (await requestResult(actions.getKey(key)) !== undefined) continue;
      const order = Number(await requestResult(sequence.add({})));
      const { action, payload } = splitEntry(entry, key, order);
      actions.put(action);
      if (payload) payloads.put(payload);
    }
    await done;
  } catch (error) {
    try {
      tx.abort();
    } catch {
      // The transaction already completed or aborted.
    }
    await done.catch(() => undefined);
    throw error;
  } finally {
    db.close();
  }
}

export async function updateOfflineAction(
  key: string,
  update: (entry: OfflineQueueEntry) => OfflineQueueEntry,
): Promise<void> {
  const db = await openDatabase();
  const tx = db.transaction(ACTIONS, "readwrite");
  const actions = tx.objectStore(ACTIONS);
  const action = await requestResult(actions.get(key)) as StoredAction | undefined;
  if (action) {
    const next = splitEntry(update(hydrate(action)), key, action.order);
    actions.put(next.action);
  }
  await transactionDone(tx);
  db.close();
}

export async function deleteOfflineAction(key: string): Promise<void> {
  const db = await openDatabase();
  const tx = db.transaction([ACTIONS, PAYLOADS], "readwrite");
  tx.objectStore(ACTIONS).delete(key);
  tx.objectStore(PAYLOADS).delete(key);
  await transactionDone(tx);
  db.close();
}

export async function clearOfflineActions(): Promise<void> {
  const db = await openDatabase();
  const tx = db.transaction([ACTIONS, PAYLOADS, SEQUENCE], "readwrite");
  tx.objectStore(ACTIONS).clear();
  tx.objectStore(PAYLOADS).clear();
  tx.objectStore(SEQUENCE).clear();
  await transactionDone(tx);
  db.close();
}
