// @ts-check
// gate-allow: raw-swap — linear wizard with no polled re-renders: every swap is
// a one-shot render driven by a user action or a terminal install event, so
// there is no tick that could clobber interaction state (renderRegion's case).
// First-run / manage-models setup surface, composed from vanilla-components on
// the shared tokens (see vc/PROVENANCE.md). Reads GET /api/setup/state, lets the
// operator pick model families + a host-valid backend, POSTs to
// /api/setup/install, and streams the Server-Sent progress into the log pane.
// Served standalone (no dashboard machinery) so it works before any backend is
// installed.
import { warmAlert, createAlertSync } from "../vc/components/alert/alert.js";
import { warmButton, createButtonSync } from "../vc/components/button/button.js";
import { warmChip, createChipSync } from "../vc/components/chip/chip.js";
import { warmField, createFieldSync } from "../vc/components/field/field.js";
import { warmPanel, createPanelSync } from "../vc/components/panel/panel.js";
import { warmSpinner, createSpinnerSync } from "../vc/components/spinner/spinner.js";

/**
 * Minimal layout-scaffolding helper (NOT styling — look comes from components +
 * tokens). Components are built via the create*Sync factories.
 * @param {string} tag
 * @param {string | null} cls
 * @param {...(Node | string)} kids
 * @returns {HTMLElement}
 */
function el(tag, cls, ...kids) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  for (const kid of kids) node.append(typeof kid === "string" ? document.createTextNode(kid) : kid);
  return node;
}

/** @param {string} id @returns {HTMLElement} */
function byId(id) {
  const node = document.getElementById(id);
  if (!node) throw new Error(`missing #${id}`);
  return node;
}

/** Per-family chosen backend, keyed by family id. */
const chosen = new Map();
// Lifetime of the page; passed to components for their event listeners.
const ac = new AbortController();

async function boot() {
  // Warm the components (so the row builders run synchronously) and fetch the
  // state concurrently — independent work, both needed before the first render.
  const warming = Promise.all([warmPanel(), warmButton(), warmField(), warmChip(), warmAlert(), warmSpinner()]);
  const sub = byId("sub");
  let state;
  try {
    const [, r] = await Promise.all([warming, fetch("/api/setup/state")]);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    state = await r.json();
  } catch (e) {
    sub.textContent = `Couldn't load setup state: ${e}`;
    return;
  }

  byId("host").textContent = `backends: ${state.available_backends.join(" · ") || "none"}`;
  byId("title").textContent = state.first_run ? "Set up TapScribe" : "Manage models";
  sub.textContent = state.first_run
    ? "Pick the model families to install. Everything runs locally; nothing leaves this machine."
    : "Add or change models. Already-installed families are marked; only new picks download.";

  renderStaleSelection(state.stale_selection);
  buildCard(state.families);
}

/**
 * Warn about families the install picker had to SKIP because their saved
 * backend left the catalog (ADR-0015).
 *
 * The picker has always reported this on stderr, which a Bundle operator never
 * sees — the Launcher pipes it to a log file. Without this banner an upgrade
 * silently stops installing someone's models with nothing to point at. The
 * remedy is re-picking below, which is exactly where they already are.
 *
 * @param {Array<Record<string, any>>|undefined} stale
 */
function renderStaleSelection(stale) {
  const host = byId("stale");
  if (!stale?.length) return;
  const names = stale.map((s) => `${s.label || s.family} (${s.backend})`).join(", ");
  host.append(
    createAlertSync({
      tone: "warn",
      message:
        `Not installed on the last run: ${names}. The saved backend is no longer ` +
        `in this version's catalog, so the family was skipped. Re-pick it below.`,
    }).el,
  );
}

/** @param {Array<Record<string, any>>} families */
function buildCard(families) {
  const list = el("div", "setup__list");
  for (const f of families) list.append(familyRow(f));

  // Built synchronously after warm + state, so it can be enabled from the start.
  const install = createButtonSync(
    {
      label: "Install & launch",
      variant: "primary",
      // .catch, not `void`: runInstall handles its own failures, but a bug in
      // its terminal path must not leave the wizard silently spinning with the
      // button disabled — re-enable and say so.
      onClick: () => runInstall(install).catch((e) => {
        console.error("install failed", e);
        install.setDisabled(false);
      }),
    },
    ac.signal,
  );
  const log = el("pre", "setup__log");
  log.id = "log";
  log.hidden = true;
  const result = el("div", "setup__result");
  result.id = "result";

  const panel = createPanelSync({ body: el("div", null, list, el("div", "setup__cta", install.el), log, result) });
  byId("card").replaceChildren(panel.el);
}

/** @param {Record<string, any>} f @returns {HTMLElement} */
function familyRow(f) {
  const box = /** @type {HTMLInputElement} */ (el("input", "fam__check"));
  box.type = "checkbox";
  // default-check Whisper / anything already installed.
  box.checked = f.family === "whisper" || Boolean(f.installed);
  box.setAttribute("aria-label", `install ${f.label}`);
  box.dataset.family = f.family;

  const chips = [createChipSync({ text: f.live ? "live + batch" : "batch only", tone: f.live ? "info" : "neutral" }).el];
  if (f.installed) chips.push(createChipSync({ text: "installed", tone: "ok" }).el);
  const name = el("div", "fam__name", f.label, ...chips);
  const meta = el("div", "fam__meta", `${(f.models || []).length} models`);

  // default backend = first host-valid; the select/chip below reflects it.
  if (f.backends.length) chosen.set(f.family, f.backends[0]);
  /** @type {Node} */
  let backend;
  if (f.backends.length > 1) {
    backend = createFieldSync(
      {
        label: `${f.label} backend`,
        type: "select",
        hideLabel: true,
        // `value` is required, not redundant: field.js does `control.value = value`,
        // and an empty value would set selectedIndex=-1 (blank select) even though
        // option 0 is `backends[0]`.
        value: f.backends[0],
        options: f.backends.map((/** @type {string} */ b) => ({ value: b, label: b })),
        onInput: (v) => chosen.set(f.family, v),
      },
      ac.signal,
    ).el;
  } else if (f.backends.length === 1) {
    backend = createChipSync({ text: f.backends[0] }).el;
  } else {
    backend = el("span", "fam__none", "—");
  }

  return el("div", "fam", box, el("div", null, name, meta), backend, el("div", "fam__size", f.size_hint || ""));
}

/** @returns {Record<string, string>} */
function selectedFamilies() {
  /** @type {Record<string, string>} */
  const out = {};
  for (const node of document.querySelectorAll(".fam__check")) {
    const cb = /** @type {HTMLInputElement} */ (node);
    const fam = cb.dataset.family;
    const backend = fam && chosen.get(fam);
    // skip a checked family with no host-valid backend rather than fabricate "cpu"
    if (cb.checked && fam && backend) out[fam] = backend;
  }
  return out;
}

/** @param {{ setDisabled: (d: boolean) => void }} install */
async function runInstall(install) {
  const families = selectedFamilies();
  const log = byId("log");
  const result = byId("result");
  result.replaceChildren();
  log.hidden = false;
  log.textContent = "";
  install.setDisabled(true);

  const busy = el("div", "setup__busy", createSpinnerSync({ size: 16 }).el, el("span", null, "Installing…"));
  result.replaceChildren(busy);

  /** @param {string} line */
  const append = (line) => {
    log.append(line + "\n"); // text-node append — O(n) total, no full-buffer rewrite
    log.scrollTop = log.scrollHeight;
  };

  let resp;
  try {
    resp = await fetch("/api/setup/install", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ families }),
    });
  } catch (e) {
    append(`request failed: ${e}`);
    return finish(false, install, result);
  }
  if (!resp.ok || !resp.body) {
    append(`install rejected: HTTP ${resp.status}`);
    return finish(false, install, result);
  }

  // Parse the SSE stream: events are "data: <json>\n\n".
  //
  // The read loop is wrapped because the install is pip-installing into the
  // very venv serving this page: a worker restart, an OOM-kill or a proxy
  // timeout breaks the stream mid-flight and rejects reader.read(). Unwrapped,
  // that escaped as an unhandled rejection — finish() never ran, the Install
  // button stayed disabled, and the first-run wizard sat on the spinner FOREVER
  // on the one surface that has no other UI to recover from (only a reload).
  // Route it through the same terminal path as a rejected POST instead.
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  let ok = false;
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let nl;
      while ((nl = buf.indexOf("\n\n")) !== -1) {
        const frame = buf.slice(0, nl);
        buf = buf.slice(nl + 2);
        if (!frame.startsWith("data:")) continue;
        let ev;
        try {
          ev = JSON.parse(frame.slice(5).trim());
        } catch {
          continue;
        }
        if (ev.phase === "log") append(ev.line);
        else if (ev.phase === "start") append("· installing…");
        else if (ev.phase === "done") {
          ok = true;
          append("· done");
        } else if (ev.phase === "error") append(`· error: ${ev.message || "exit " + ev.returncode}`);
      }
    }
  } catch (e) {
    append(`install stream failed: ${e}`);
    ok = false;
  } finally {
    finish(ok, install, result);
  }
}

/**
 * Render the terminal install result.
 * @param {boolean} ok
 * @param {{ setDisabled: (d: boolean) => void }} install
 * @param {HTMLElement} result
 */
function finish(ok, install, result) {
  if (ok) {
    const open = createButtonSync({ label: "Open the dashboard →", variant: "primary", href: "/" });
    result.replaceChildren(createAlertSync({ tone: "ok", message: "Installed." }).el, open.el);
  } else {
    result.replaceChildren(createAlertSync({ tone: "bad", message: "Install failed — see the log above." }).el);
    install.setDisabled(false);
  }
}

void boot();
