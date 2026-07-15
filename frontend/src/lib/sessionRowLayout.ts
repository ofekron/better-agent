export function sessionRowLayoutId(sessionId: string, groupedView: boolean): string | undefined {
  if (groupedView) return undefined;
  return `session-row-${sessionId}`;
}
