const STATE_KEY = "better_agent_offline_sync_state";

function readState() {
  const stored = CapacitorKV.get(STATE_KEY);
  if (!stored?.value) return null;
  try {
    return JSON.parse(stored.value);
  } catch {
    CapacitorKV.remove(STATE_KEY);
    return null;
  }
}

addEventListener("updateOfflineState", (resolve, reject, args) => {
  try {
    if (!args || typeof args.state !== "string") {
      reject(new Error("Offline sync state is required"));
      return;
    }
    const incoming = JSON.parse(args.state);
    const current = readState();
    const acknowledged = Array.isArray(current?.acknowledged) ? current.acknowledged : [];
    const acknowledgedKeys = new Set(
      acknowledged.map((item) => `${item.sessionId}\u0000${item.clientId}`),
    );
    incoming.actions = incoming.actions.filter((action) => {
      const sessionId = action.type === "create_session" ? action.session?.id : action.sessionId;
      return !acknowledgedKeys.has(`${sessionId}\u0000${action.clientId}`);
    });
    incoming.acknowledged = acknowledged;
    CapacitorKV.set(STATE_KEY, JSON.stringify(incoming));
    resolve();
  } catch (error) {
    reject(error);
  }
});

addEventListener("getOfflineAcknowledgements", (resolve, reject) => {
  try {
    const state = readState();
    resolve({ acknowledged: Array.isArray(state?.acknowledged) ? state.acknowledged : [] });
  } catch (error) {
    reject(error);
  }
});

addEventListener("clearOfflineAcknowledgements", (resolve, reject) => {
  try {
    const state = readState();
    if (state) {
      state.acknowledged = [];
      CapacitorKV.set(STATE_KEY, JSON.stringify(state));
    }
    resolve();
  } catch (error) {
    reject(error);
  }
});

addEventListener("clearOfflineState", (resolve, reject) => {
  try {
    CapacitorKV.remove(STATE_KEY);
    resolve();
  } catch (error) {
    reject(error);
  }
});

addEventListener("syncOfflineActions", async (resolve, reject) => {
  try {
    const state = readState();
    if (!state || !Array.isArray(state.actions) || state.actions.length === 0) {
      resolve();
      return;
    }
    if (typeof state.serverUrl !== "string" || typeof state.accessToken !== "string") {
      resolve();
      return;
    }
    if (!CapacitorDevice.getNetworkStatus().connected) {
      resolve();
      return;
    }

    const response = await fetch(`${state.serverUrl}/api/offline-actions/batch`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${state.accessToken}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ actions: state.actions }),
    });
    if (response.status === 401 || response.status === 403) {
      resolve();
      return;
    }
    if (!response.ok) {
      reject(new Error(`Offline sync failed with HTTP ${response.status}`));
      return;
    }

    const payload = await response.json();
    if (!payload || !Array.isArray(payload.results)) {
      reject(new Error("Offline sync returned an invalid response"));
      return;
    }
    const acceptedIndexes = new Set(
      payload.results
        .filter((result) => result?.accepted === true && Number.isInteger(result.index))
        .map((result) => result.index),
    );
    const accepted = state.actions
      .map((action, index) => ({ action, index }))
      .filter(({ index }) => acceptedIndexes.has(index))
      .map(({ action }) => ({
        sessionId: action.type === "create_session" ? action.session?.id : action.sessionId,
        clientId: action.clientId,
      }))
      .filter((item) => typeof item.sessionId === "string" && typeof item.clientId === "string");
    state.actions = state.actions.filter((_, index) => !acceptedIndexes.has(index));
    state.acknowledged = [...(Array.isArray(state.acknowledged) ? state.acknowledged : []), ...accepted];
    CapacitorKV.set(STATE_KEY, JSON.stringify(state));
    resolve();
  } catch (error) {
    reject(error);
  }
});
