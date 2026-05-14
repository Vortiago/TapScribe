// Pure formatters and DOM-safe escapers used across the dashboard.
// No DOM dependency, no shared state — safe to unit-test in isolation.

export function escapeHtml(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );
}

export function cssEscape(s) {
  return String(s).replace(/["\\]/g, "\\$&");
}

export function fmtBytes(b) {
  if (b == null) return "0 B";
  if (b < 1024) return b + " B";
  if (b < 1024 * 1024) return (b / 1024).toFixed(1) + " KB";
  return (b / 1024 / 1024).toFixed(2) + " MB";
}

export function fmtDur(s) {
  if (s == null || isNaN(s)) return "?";
  return s < 60 ? s.toFixed(2) + " s" : Math.floor(s / 60) + "m " + (s % 60).toFixed(1) + "s";
}

export function fmtMs(ms) {
  if (ms == null) return "?";
  return ms < 1000 ? ms + " ms" : (ms / 1000).toFixed(1) + " s";
}

export function fmtClock(iso) {
  if (!iso) return "?";
  return iso.slice(11, 19);
}

export function fmtElapsed(sec) {
  if (sec == null) return "—";
  const p = (n) => String(n).padStart(2, "0");
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  return `${p(h)}:${p(m)}:${p(s)}`;
}

export function fmtElapsedShort(sec) {
  if (sec == null || isNaN(sec)) return "0:00";
  const total = Math.max(0, Math.floor(sec));
  const m = Math.floor(total / 60);
  const s = total % 60;
  return m + ":" + String(s).padStart(2, "0");
}

export function truncMid(s, max) {
  if (!s) return "";
  if (s.length <= max) return s;
  const half = Math.floor((max - 1) / 2);
  return s.slice(0, half) + "…" + s.slice(-half);
}

// "2026-05-12T09-19-55Z" → "05-12 09:19"
export function fmtSessionLabel(s) {
  if (!s || s.length < 16) return s || "";
  return s.slice(5, 10) + " " + s.slice(11, 13) + ":" + s.slice(14, 16);
}
