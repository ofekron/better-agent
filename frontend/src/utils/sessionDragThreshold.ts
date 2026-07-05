const AGENT_BOARD_SESSION_DRAG_START_DISTANCE_PX = 48;

export type SessionDragPoint = {
  clientX: number;
  clientY: number;
};

export function shouldStartAgentBoardSessionDrag(
  start: SessionDragPoint | null,
  current: SessionDragPoint,
  thresholdPx = AGENT_BOARD_SESSION_DRAG_START_DISTANCE_PX,
): boolean {
  if (!start) return false;
  const dx = current.clientX - start.clientX;
  const dy = current.clientY - start.clientY;
  return Math.hypot(dx, dy) >= thresholdPx;
}
