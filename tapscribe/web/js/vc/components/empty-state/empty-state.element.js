// from vanilla-components/components/empty-state/empty-state.element.js@9f20766 - re-copy to update, don't fork
// @ts-check
// <vc-empty-state> — declarative face of createEmptyState.
//   <vc-empty-state icon="📭" title="No runs yet" detail="Trigger one to begin"></vc-empty-state>
import { warmEmptyState, createEmptyStateSync } from "./empty-state.js";
import { defineElement } from "../../lib/element.js";

defineElement("vc-empty-state", { warm: warmEmptyState, sync: createEmptyStateSync }, {
  attrs: ["icon", "title", "detail"],
});
