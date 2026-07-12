// from vanilla-components/components/progress/progress.element.js@9f20766 - re-copy to update, don't fork
// @ts-check
// <vc-progress> — declarative face of createProgress.
//   <vc-progress value="40" max="100" tone="ok" label="Uploading"></vc-progress>
import { warmProgress, createProgressSync } from "./progress.js";
import { defineElement } from "../../lib/element.js";

defineElement("vc-progress", { warm: warmProgress, sync: createProgressSync }, {
  attrs: ["value", "max", "tone", "label"],
  numbers: ["value", "max"],
  setters: { value: "setValue" },
});
