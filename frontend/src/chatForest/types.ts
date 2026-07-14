export interface ForestPrompt {
  id: string;
  text: string;
  payload: Record<string, unknown>;
}

export interface ForestExplanation extends ForestPrompt {}

export interface ForestWork {
  id: string;
  kind: string;
  payload: Record<string, unknown>;
}

export interface PromptTree {
  id: string;
  prompt: ForestPrompt;
  turn_id: string | null;
  explanations: ForestExplanation[];
  work: ForestWork[];
  status: string;
  queued: boolean;
  partial: boolean;
  has_late_output: boolean;
  events_collapsed_by_default: boolean;
  prompt_text_collapsed_by_default: boolean;
  collapsed_preview: string;
}

export interface ChatForest {
  root_id: string;
  root_generation: number;
  canonical_through_seq: number;
  trees: PromptTree[];
}

interface ProjectionBase {
  found: true;
  schema_version: number;
  root_generation: number;
  epoch: string;
  canonical_through_seq: number;
  checksum: string;
}

export interface ForestSnapshot extends ProjectionBase {
  kind: "snapshot";
  revision: number;
  forest: ChatForest;
}

export interface ForestDelta extends ProjectionBase {
  kind: "delta";
  base_revision: number;
  target_revision: number;
  upsert_trees: PromptTree[];
  remove_tree_ids: string[];
}

export type ForestResponse = { found: false } | ForestSnapshot | ForestDelta;

export interface ForestState {
  status: "idle" | "loading" | "ready" | "error";
  sessionId: string | null;
  epoch: string | null;
  revision: number;
  checksum: string | null;
  forest: ChatForest | null;
  error: string | null;
}
