import type { ChatMessage, EntityBlock, OrchestrationMode, WSEvent, WorkerPanel } from "../types";
import type { OrchestrationStrategy } from "./OrchestrationStrategy";
import { tagEvents, groupByEntity, dedupeWorkerPanels } from "../utils/mergeEvents";
import { unwrapTypedAgentMessageEnvelope } from "../utils/workerEventEnvelope";

/**
 * Single orchestration strategy, parameterized by mode. Both manager and
 * native modes store the primary agent's events flat on `message.events`
 * and the per-turn primary CLI session id on `message.agent_session_id`.
 * Manager mode additionally renders events + workers interleaved as entity
 * blocks; native renders linearly unless worker/session panels need
 * `insert_at` interleaving.
 */
export class Strategy implements OrchestrationStrategy {
  readonly mode: OrchestrationMode;

  constructor(mode: OrchestrationMode) {
    this.mode = mode;
  }

  hasScopeWrapper(message: ChatMessage): boolean {
    void message;
    return this.mode === "team";
  }

  getEvents(message: ChatMessage): WSEvent[] {
    return message.events ?? [];
  }

  buildEntityBlocks(
    message: ChatMessage,
    workers: ChatMessage["workers"],
  ): EntityBlock[] | undefined {
    const events = message.events ?? [];
    const panels = dedupeWorkerPanels(workers ?? []);
    if (events.length === 0 && panels.length === 0) return undefined;
    if (this.mode !== "team" && panels.length === 0) return undefined;
    return groupByEntity(tagEvents(events, panels));
  }

  applyLiveEvent(message: ChatMessage, event: WSEvent): ChatMessage {
    const typedEnvelope = unwrapTypedAgentMessageEnvelope(event);
    if (typedEnvelope) return this.applyLiveEvent(message, typedEnvelope);

    const etype = event.type;

    if (etype === "turn_start") {
      const sid = (event.data.manager_session_id as string | null) ?? null;
      return { ...message, agent_session_id: sid };
    }

    if (
      etype === "agent_message" ||
      etype === "manager_event" ||
      etype === "steer_prompt"
    ) {
      // agent_message / steer_prompt: canonical render payloads.
      // manager_event: legacy backward compat — unwrap inner event.
      const ev =
        etype === "manager_event"
          ? (event.data as { event?: WSEvent }).event
          : event;
      if (!ev) return message;

      const uuid = ev.data?.uuid as string | undefined;
      const evs = message.events ?? [];
      let nextEvs = evs;
      if (uuid) {
        const idx = evs.findIndex((e) => e.data?.uuid === uuid);
        if (idx !== -1) {
          nextEvs = [...evs];
          nextEvs[idx] = ev;
        } else {
          nextEvs = [...evs, ev];
        }
      } else {
        nextEvs = [...evs, ev];
      }
      return { ...message, events: nextEvs };
    }

    if (etype === "todos_snapshot") {
      const inner: WSEvent = {
        type: "todos_snapshot",
        data: event.data,
      };
      return { ...message, events: [...(message.events ?? []), inner] };
    }

    if (
      etype === "worker_prep_start" ||
      etype === "worker_prep_event" ||
      etype === "worker_prep_complete" ||
      etype === "worker_prep_cancelled"
    ) {
      return { ...message, events: [...(message.events ?? []), event] };
    }

    if (etype === "turn_complete") {
      const sid = (event.data as { session_id?: string }).session_id;
      if (!sid) return message;
      return { ...message, agent_session_id: sid };
    }

    if (etype === "worker_start") {
      const d = event.data as {
        delegation_id: string;
        worker_session_id: string | null;
        worker_description: string;
        panel_kind?: WorkerPanel["panel_kind"];
        started_at?: string;
        insert_at?: number;
        is_new: boolean;
        instructions_preview: string;
        orchestration_mode?: OrchestrationMode;
        run_mode?: string;
      };
      const existing = message.workers ?? [];
      if (existing.some((p) => p.delegation_id === d.delegation_id)) {
        return message;
      }
      const newPanel: WorkerPanel = {
        delegation_id: d.delegation_id,
        worker_session_id: d.worker_session_id ?? "",
        worker_description: d.worker_description,
        panel_kind: d.panel_kind,
        started_at: d.started_at,
        insert_at: d.insert_at,
        is_new: d.is_new,
        instructions_preview: d.instructions_preview,
        orchestration_mode: d.orchestration_mode,
        run_mode: d.run_mode,
        events: [],
      };
      return { ...message, workers: [...existing, newPanel] };
    }

    if (etype === "worker_event") {
      const d = event.data as { delegation_id: string; event?: WSEvent };
      if (!d.event) return message;
      const workers = message.workers ?? [];
      const idx = workers.findIndex((p) => p.delegation_id === d.delegation_id);
      if (idx === -1) return message;
      const next = [...workers];
      next[idx] = { ...next[idx], events: [...next[idx].events, d.event] };
      return { ...message, workers: next };
    }

    if (etype === "worker_complete") {
      const d = event.data as {
        delegation_id: string;
        worker_session_id?: string | null;
        jsonl_path?: string | null;
        new_byte_offset?: number;
        token_usage?: Record<string, unknown>;
        success?: boolean;
        error?: string | null;
        fork_agent_sid?: string | null;
        run_mode?: string;
      };
      const workers = message.workers ?? [];
      const idx = workers.findIndex((p) => p.delegation_id === d.delegation_id);
      if (idx === -1) return message;
      const next = [...workers];
      const panel = { ...next[idx] };
      if (d.worker_session_id) panel.worker_session_id = d.worker_session_id;
      panel.jsonl_path = d.jsonl_path ?? null;
      panel.new_byte_offset = d.new_byte_offset;
      panel.token_usage = d.token_usage as WorkerPanel["token_usage"];
      panel.success = d.success;
      panel.error = d.error ?? null;
      if (d.fork_agent_sid) panel.fork_agent_sid = d.fork_agent_sid;
      if (d.run_mode) panel.run_mode = d.run_mode;
      next[idx] = panel;
      return { ...message, workers: next };
    }

    return message;
  }
}
