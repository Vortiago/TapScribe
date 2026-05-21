// @ts-check
// Thin wrappers over the backend's HTTP API. Each helper returns the
// parsed JSON / text or throws an Error with status + server-provided detail.

/** @param {Response} r */
async function _unwrap(r) {
  if (r.ok) {
    const ct = r.headers.get("content-type") || "";
    return ct.includes("application/json") ? r.json() : r.text();
  }
  let detail = r.statusText;
  try { detail = (await r.json()).detail || detail; } catch { /* not JSON */ }
  throw new Error(`${r.status} ${detail}`);
}

/** @param {unknown} body */
const _body = (body) => ({
  headers: { "content-type": "application/json" },
  body: JSON.stringify(body ?? {}),
});

export const fetchState = () => fetch("/api/state", { cache: "no-store" }).then(_unwrap);
/**
 * @param {string} url
 * @param {unknown} [body]
 */
export const postJson = (url, body) => fetch(url, { method: "POST", ..._body(body) }).then(_unwrap);
/**
 * @param {string} url
 * @param {unknown} [body]
 */
export const putJson  = (url, body) => fetch(url, { method: "PUT",  ..._body(body) }).then(_unwrap);
/** @param {string} url */
export const del      = (url)       => fetch(url, { method: "DELETE" }).then(_unwrap);

// Wire a textarea + save button to PUT /api/config/{key}. Used by both the
// "default config" card editors and the live-channel's init-prompt
// expandable. Manages the status badge lifecycle (saving / saved / failed)
// and clears the success badge after a short delay.
/**
 * @param {{
 *   key: string,
 *   btn: HTMLButtonElement,
 *   textarea: HTMLTextAreaElement | HTMLInputElement | null,
 *   status: HTMLElement | null,
 *   onSuccess?: ((value: string) => void) | undefined,
 * }} opts
 */
export function wireConfigSave({ key, btn, textarea, status, onSuccess }) {
  btn.addEventListener("click", async () => {
    if (!textarea || !status) return;
    btn.disabled = true;
    status.textContent = "saving…";
    try {
      await putJson(`/api/config/${key}`, { content: textarea.value });
      status.textContent = "saved";
      onSuccess?.(textarea.value);
      setTimeout(() => { if (status.textContent === "saved") status.textContent = ""; }, 1500);
    } catch (e) {
      status.textContent = `failed: ${String(e).replace(/^Error:\s*/, "")}`;
    } finally {
      btn.disabled = false;
    }
  });
}
