import type { ForestResponse, ForestSnapshot, ForestState } from "./types";

export type ForestAction =
  | { type: "load"; sessionId: string }
  | { type: "response"; sessionId: string; response: ForestResponse }
  | { type: "error"; sessionId: string; error: string };

export const emptyForestState: ForestState = {
  status: "idle",
  sessionId: null,
  epoch: null,
  revision: 0,
  checksum: null,
  forest: null,
  error: null,
};

function fromSnapshot(sessionId: string, snapshot: ForestSnapshot): ForestState {
  return {
    status: "ready",
    sessionId,
    epoch: snapshot.epoch,
    revision: snapshot.revision,
    checksum: snapshot.checksum,
    forest: snapshot.forest,
    error: null,
  };
}

export function reduceChatForest(state: ForestState, action: ForestAction): ForestState {
  if (action.type === "load") {
    return { ...emptyForestState, status: "loading", sessionId: action.sessionId };
  }
  if (action.sessionId !== state.sessionId) return state;
  if (action.type === "error") {
    return { ...state, status: "error", error: action.error };
  }
  const response = action.response;
  if (!response.found) return { ...emptyForestState, sessionId: action.sessionId };
  if (response.kind === "snapshot") return fromSnapshot(action.sessionId, response);
  if (!state.forest || response.epoch !== state.epoch) {
    return { ...state, status: "loading", error: null };
  }
  if (response.base_revision !== state.revision) {
    return { ...state, status: "loading", error: null };
  }
  if (response.target_revision === state.revision) return state;
  const removed = new Set(response.remove_tree_ids);
  const upserts = new Map(response.upsert_trees.map((tree) => [tree.id, tree]));
  const trees = state.forest.trees
    .filter((tree) => !removed.has(tree.id))
    .map((tree) => upserts.get(tree.id) ?? tree);
  for (const tree of response.upsert_trees) {
    if (!state.forest.trees.some((existing) => existing.id === tree.id)) trees.push(tree);
  }
  return {
    ...state,
    status: "ready",
    revision: response.target_revision,
    checksum: response.checksum,
    forest: {
      ...state.forest,
      root_generation: response.root_generation,
      canonical_through_seq: response.canonical_through_seq,
      trees,
    },
    error: null,
  };
}
