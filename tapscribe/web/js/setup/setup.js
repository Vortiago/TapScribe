// @ts-check
// First-run / manage-models setup surface. Reads GET /api/setup/state, lets the
// operator pick model families + a host-valid backend, then POSTs to
// /api/setup/install and streams the Server-Sent progress into the log pane.
// Plain vanilla DOM — served standalone (no dashboard machinery), so it works
// before any backend is installed. The polished centred-console look lives in
// prototypes/setup (variant D); this is the functional first cut.

/**
 * @param {string} tag
 * @param {Record<string, unknown>} [props]
 * @param {...(Node | string | null | false | Array<Node | string | null | false>)} kids
 * @returns {HTMLElement}
 */
function el(tag, props = {}, ...kids) {
  const node = document.createElement(tag);
  const any = /** @type {Record<string, unknown>} */ (/** @type {unknown} */ (node));
  for (const [k, v] of Object.entries(props)) {
    if (v == null || v === false) continue;
    if (k === "class") node.className = String(v);
    else if (k === "dataset") Object.assign(node.dataset, v);
    else if (k.startsWith("on") && typeof v === "function") {
      node.addEventListener(k.slice(2).toLowerCase(), /** @type {EventListener} */ (v));
    } else if (k in node) any[k] = v;
    else node.setAttribute(k, String(v));
  }
  for (const kid of kids.flat()) {
    if (kid == null || kid === false) continue;
    node.append(typeof kid === "string" ? document.createTextNode(kid) : kid);
  }
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

async function boot() {
  const sub = byId("sub");
  let state;
  try {
    const r = await fetch("/api/setup/state");
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

  renderPicker(state.families);
  const install = /** @type {HTMLButtonElement} */ (byId("install"));
  install.disabled = false;
  install.addEventListener("click", () => {
    void runInstall();
  });
}

/** @param {Array<Record<string, any>>} families */
function renderPicker(families) {
  const rows = families.map((f) => {
    // default backend = first host-valid; default-check Whisper / installed.
    if (f.backends.length) chosen.set(f.family, f.backends[0]);
    const cap = el("span", { class: f.live ? "chip chip-info" : "chip chip-muted" }, f.live ? "live + batch" : "batch only");
    const installed = f.installed ? el("span", { class: "chip chip-ok" }, "installed") : null;
    const check = el("input", {
      type: "checkbox",
      checked: f.family === "whisper" || Boolean(f.installed),
      "aria-label": `install ${f.label}`,
      dataset: { family: f.family },
    });
    const backendSel = el(
      "select",
      {
        "aria-label": `${f.label} backend`,
        disabled: f.backends.length <= 1,
        onchange: (/** @type {Event} */ e) => {
          chosen.set(f.family, /** @type {HTMLSelectElement} */ (e.target).value);
        },
      },
      ...f.backends.map((/** @type {string} */ b) => el("option", { value: b }, b)),
    );
    return el(
      "tr",
      {},
      el("td", {}, check),
      el("td", {}, el("div", { class: "name" }, f.label, cap, installed), el("div", { class: "blurb" }, `${(f.models || []).length} models`)),
      el("td", {}, f.backends.length ? backendSel : el("span", { class: "blurb" }, "—")),
      el("td", { class: "sz" }, f.size_hint || ""),
    );
  });
  byId("picker").replaceChildren(
    el(
      "table",
      {},
      el("thead", {}, el("tr", {}, el("th", {}, ""), el("th", {}, "family"), el("th", {}, "backend"), el("th", { class: "sz" }, "size"))),
      el("tbody", {}, rows),
    ),
  );
}

/** @returns {Record<string, string>} */
function selectedFamilies() {
  /** @type {Record<string, string>} */
  const out = {};
  for (const box of document.querySelectorAll('#picker input[type="checkbox"]')) {
    const cb = /** @type {HTMLInputElement} */ (box);
    const fam = cb.dataset.family;
    if (cb.checked && fam) out[fam] = chosen.get(fam) || "cpu";
  }
  return out;
}

async function runInstall() {
  const families = selectedFamilies();
  const log = byId("log");
  const result = byId("result");
  const install = /** @type {HTMLButtonElement} */ (byId("install"));
  result.replaceChildren();
  log.hidden = false;
  log.textContent = "";
  install.disabled = true;

  /** @param {string} line */
  const append = (line) => {
    log.textContent = (log.textContent || "") + line + "\n";
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
    install.disabled = false;
    return;
  }
  if (!resp.ok || !resp.body) {
    append(`install rejected: HTTP ${resp.status}`);
    install.disabled = false;
    return;
  }

  // Parse the SSE stream: events are "data: <json>\n\n".
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  let ok = false;
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

  if (ok) {
    result.replaceChildren(el("div", { class: "done" }, "✓ Installed."), el("a", { class: "dash", href: "/" }, "Open the dashboard →"));
  } else {
    result.replaceChildren(el("div", { class: "err" }, "Install failed — see the log above."));
    install.disabled = false;
  }
}

void boot();
