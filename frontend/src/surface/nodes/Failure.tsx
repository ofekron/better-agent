import type { FailurePayloadWire, NodeWire } from "../../adapter/wire";
import { FailureChip } from "../leaf/Chips";

export function FailureView({ node }: { node: NodeWire }) {
  const payload = node.payload as FailurePayloadWire | null;
  if (!payload) return null;
  return <FailureChip payload={payload} />;
}
