export type ComposerSendActionId = "send" | "queue" | "steer" | "interrupt";
export type ComposerActionSurface = "primary" | "desktop" | "mobileTop" | "mobileOverflow";

export interface ComposerSendAction {
  id: ComposerSendActionId;
  label: string;
  title?: string;
  run: () => void;
  surfaces: readonly ComposerActionSurface[];
}

export interface ComposerActionRegistryInput {
  running: boolean;
  steerable: boolean;
  send: () => void;
  steer: () => void;
  interrupt?: () => void;
  labels: Record<ComposerSendActionId, string>;
  steerTitle?: string;
  interruptTitle?: string;
}

export function buildComposerActionRegistry(
  input: ComposerActionRegistryInput,
): ComposerSendAction[] {
  if (!input.running) {
    return [{
      id: "send",
      label: input.labels.send,
      run: input.send,
      surfaces: ["primary"],
    }];
  }

  const actions: ComposerSendAction[] = [];
  if (input.steerable) {
    actions.push({
      id: "steer",
      label: input.labels.steer,
      title: input.steerTitle,
      run: input.steer,
      surfaces: ["primary", "mobileTop"],
    });
    actions.push({
      id: "queue",
      label: input.labels.queue,
      run: input.send,
      surfaces: ["desktop", "mobileTop"],
    });
  } else {
    actions.push({
      id: "queue",
      label: input.labels.queue,
      run: input.send,
      surfaces: ["primary"],
    });
  }
  if (input.interrupt) {
    actions.push({
      id: "interrupt",
      label: input.labels.interrupt,
      title: input.interruptTitle,
      run: input.interrupt,
      surfaces: ["desktop", "mobileOverflow"],
    });
  }
  return actions;
}

export function composerActionsForSurface(
  actions: readonly ComposerSendAction[],
  surface: ComposerActionSurface,
): ComposerSendAction[] {
  return actions.filter((action) => action.surfaces.includes(surface));
}
