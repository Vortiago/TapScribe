// from vanilla-components/components/spinner/spinner.element.js@9f20766 - re-copy to update, don't fork
// @ts-check
// <vc-spinner> — declarative face of createSpinner.
//   <vc-spinner size="24" label="Loading"></vc-spinner>
import { warmSpinner, createSpinnerSync } from "./spinner.js";
import { defineElement } from "../../lib/element.js";

defineElement("vc-spinner", { warm: warmSpinner, sync: createSpinnerSync }, {
  attrs: ["size", "label"],
  numbers: ["size"],
});
