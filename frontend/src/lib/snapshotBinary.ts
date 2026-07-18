export const SNAPSHOT_BINARY_SUBPROTOCOL = "better-agent.snapshot.binary-v1";
export const SNAPSHOT_BINARY_ENCODING = "binary-v1";

const HEADER_BYTES = 32;
const MAX_PAYLOAD_BYTES = 180 * 1024;

export type SnapshotBinaryChunk = {
  snapshotId: string;
  index: number;
  payload: Uint8Array;
};

export function decodeSnapshotBinaryChunk(frame: ArrayBuffer): SnapshotBinaryChunk | null {
  if (frame.byteLength <= HEADER_BYTES || frame.byteLength > HEADER_BYTES + MAX_PAYLOAD_BYTES) {
    return null;
  }
  const bytes = new Uint8Array(frame);
  if (bytes[0] !== 66 || bytes[1] !== 65 || bytes[2] !== 83 || bytes[3] !== 78
    || bytes[4] !== 1 || bytes[5] !== 1 || bytes[6] !== 0 || bytes[7] !== 0) {
    return null;
  }
  const view = new DataView(frame);
  const index = view.getUint32(24, false);
  const payloadLength = view.getUint32(28, false);
  if (payloadLength < 1 || payloadLength > MAX_PAYLOAD_BYTES
    || frame.byteLength !== HEADER_BYTES + payloadLength) return null;
  let snapshotId = "";
  for (let offset = 8; offset < 24; offset += 1) {
    snapshotId += bytes[offset].toString(16).padStart(2, "0");
  }
  return {
    snapshotId,
    index,
    payload: new Uint8Array(frame, HEADER_BYTES, payloadLength),
  };
}
