// Thin wrappers over the backend's HTTP API. Each function returns the
// parsed JSON or throws an Error with a useful message.

async function _jsonOrThrow(r) {
  if (r.ok) {
    const ct = r.headers.get("content-type") || "";
    if (ct.includes("application/json")) return r.json();
    return r.text();
  }
  let detail = r.statusText;
  try {
    const j = await r.json();
    if (j && j.detail) detail = j.detail;
  } catch (_) {
    // body wasn't JSON; keep statusText
  }
  throw new Error(r.status + " " + detail);
}

export async function fetchState() {
  const r = await fetch("/api/state", { cache: "no-store" });
  if (!r.ok) throw new Error(r.status);
  return r.json();
}

export async function postJson(url, body) {
  const r = await fetch(url, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  return _jsonOrThrow(r);
}

export async function putJson(url, body) {
  const r = await fetch(url, {
    method: "PUT",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  return _jsonOrThrow(r);
}

export async function del(url) {
  const r = await fetch(url, { method: "DELETE" });
  return _jsonOrThrow(r);
}
