// Thin wrappers over the backend's HTTP API. Each helper returns the
// parsed JSON / text or throws an Error with status + server-provided detail.

async function _unwrap(r) {
  if (r.ok) {
    const ct = r.headers.get("content-type") || "";
    return ct.includes("application/json") ? r.json() : r.text();
  }
  let detail = r.statusText;
  try { detail = (await r.json()).detail || detail; } catch { /* not JSON */ }
  throw new Error(`${r.status} ${detail}`);
}

const _body = (body) => ({
  headers: { "content-type": "application/json" },
  body: JSON.stringify(body ?? {}),
});

export const fetchState = () => fetch("/api/state", { cache: "no-store" }).then(_unwrap);
export const postJson = (url, body) => fetch(url, { method: "POST", ..._body(body) }).then(_unwrap);
export const putJson  = (url, body) => fetch(url, { method: "PUT",  ..._body(body) }).then(_unwrap);
export const del      = (url)       => fetch(url, { method: "DELETE" }).then(_unwrap);
