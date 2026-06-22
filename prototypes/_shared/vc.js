// Thin façade over the vendored vanilla-components (./vc/). Imports the subset
// the prototypes use, exposes warmAll() (await once at startup so the synchronous
// create*Sync builders below can run inside re-render loops), and re-exports the
// Sync builders under short names. Paths are relative to THIS file, so any
// importer (index.html, launcher.js, setup/src/*) gets the same resolution.

import { warmButton, createButtonSync } from "./vc/components/button/button.js";
import { warmProgress, createProgressSync } from "./vc/components/progress/progress.js";
import { warmChip, createChipSync } from "./vc/components/chip/chip.js";
import { warmKvRow, createKvRowSync } from "./vc/components/kv-row/kv-row.js";
import { warmSegmentedControl, createSegmentedControlSync } from "./vc/components/segmented-control/segmented-control.js";
import { warmStatusDot, createStatusDotSync } from "./vc/components/status-dot/status-dot.js";
import { warmListRow, createListRowSync } from "./vc/components/list-row/list-row.js";
import { warmStatCard, createStatCardSync } from "./vc/components/stat-card/stat-card.js";
import { warmPanel, createPanelSync } from "./vc/components/panel/panel.js";
import { warmViewHeader, createViewHeaderSync } from "./vc/components/view-header/view-header.js";
import { warmAlert, createAlertSync } from "./vc/components/alert/alert.js";
import { warmMenu, createMenuSync } from "./vc/components/menu/menu.js";

export const VC = {
  button: createButtonSync,
  progress: createProgressSync,
  chip: createChipSync,
  kvRow: createKvRowSync,
  seg: createSegmentedControlSync,
  dot: createStatusDotSync,
  listRow: createListRowSync,
  statCard: createStatCardSync,
  panel: createPanelSync,
  viewHeader: createViewHeaderSync,
  alert: createAlertSync,
  menu: createMenuSync,
};

/** Await once before the first synchronous create*Sync call. */
export function warmAll() {
  return Promise.all([
    warmButton(), warmProgress(), warmChip(), warmKvRow(), warmSegmentedControl(),
    warmStatusDot(), warmListRow(), warmStatCard(), warmPanel(), warmViewHeader(),
    warmAlert(), warmMenu(),
  ]);
}
