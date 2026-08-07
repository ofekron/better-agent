// Dispatches ANY NodeWire to its per-kind renderer by `kind` — the single
// switch every recursive render point (TurnView, Container's NodeList)
// goes through, so adding a 25th kind means editing exactly one place.
// Unrecognized kinds fall through to the `unknown` renderer's chrome
// (ADR 0006 §0: "Clients render unknown node kinds with the generic
// renderer... additive evolution must never break an older UI").

import type { NodeWire, RunWire } from "../adapter/wire";
import type { SurfaceStore } from "./state";
import type { RenderMode } from "./nodes/Container";
import { ExplanationView, SubAgentTurnView } from "./nodes/Container";
import { TypedPromptView } from "./nodes/TypedPrompt";
import { AssistantTextView, SteeringMessageView, ThinkingView } from "./nodes/ContentLeaves";
import { ToolInteractionView } from "./nodes/ToolInteraction";
import { ModelChangeView, HarnessChangeView } from "./nodes/RuntimeChanged";
import { ResultView } from "./nodes/Result";
import { CompactionView, ContinuationSessionView } from "./nodes/Continuation";
import { FailureView } from "./nodes/Failure";
import { LifecycleNoticeView } from "./nodes/LifecycleNotice";
import { DiagnosticView, FactView, UnknownView, WorkerInteractionView } from "./nodes/Misc";
import { UserInteractionActionView } from "./nodes/UserInteractionAction";

export interface NodeViewProps {
  node: NodeWire;
  /** Only meaningful (and required) for the two container kinds
   * (explanation, the SubAgentTurn family) — every leaf kind ignores it. */
  containerMode?: RenderMode;
  store?: SurfaceStore;
  runsById?: ReadonlyMap<string, RunWire>;
}

export function NodeView({ node, containerMode, store, runsById }: NodeViewProps) {
  switch (node.kind) {
    case "explanation":
      if (!store || !runsById) return null;
      return <ExplanationView node={node} store={store} containerMode={containerMode ?? "collapsed"} runsById={runsById} />;
    case "native_subagent_turn":
    case "worker_turn":
    case "sub_session_turn":
    case "session_turn":
      if (!store || !runsById) return null;
      return <SubAgentTurnView node={node} store={store} containerMode={containerMode ?? "collapsed"} runsById={runsById} />;
    case "typed_prompt":
      return <TypedPromptView node={node} />;
    case "assistant_text":
      return <AssistantTextView node={node} />;
    case "thinking":
      return <ThinkingView node={node} />;
    case "tool_interaction":
      return <ToolInteractionView node={node} />;
    case "steering_message":
      return <SteeringMessageView node={node} />;
    case "worker_interaction":
      return <WorkerInteractionView node={node} />;
    case "model_change":
      return <ModelChangeView node={node} />;
    case "harness_change":
      return <HarnessChangeView node={node} />;
    case "result":
      return <ResultView node={node} />;
    case "compaction":
      return <CompactionView node={node} />;
    case "continuation_session":
      return <ContinuationSessionView node={node} />;
    case "failure":
      return <FailureView node={node} />;
    case "diagnostic":
      return <DiagnosticView node={node} />;
    case "user_interaction":
      return <UserInteractionActionView node={node} />;
    case "lifecycle_notice":
      return <LifecycleNoticeView node={node} />;
    case "fact":
      return <FactView node={node} />;
    case "unknown":
      return <UnknownView node={node} />;
    // `turn` and `instruction_widget` are never rendered through NodeView
    // — TurnView.tsx and ChatSurfaceView.tsx own their dedicated render
    // paths respectively (turn.child_manifest/turn.results are read
    // directly off TurnEntry, never traversed as a generic node).
    default:
      return null;
  }
}
