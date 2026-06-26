import { extId } from "./extensionIds";

// Virtual singleton id for the project-structure edit session. The extension
// id is resolved at runtime (private id fetched at bootstrap), so this must be
// a function — not a module-load-time constant.
export const editSingletonId = () =>
  `virtual:${extId("projectStructure")}:project-structure-edit`;
