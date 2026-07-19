// Zero-dep static server for the popup E2E: serves the bridge directory so
// Playwright can drive popup.html in a real browser (the module graph, the
// vendored components' fetch-loaded HTML/CSS, control-client.js as a classic
// global). Not shipped — dev-only, the webServer for playwright.config.js.
import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { join, relative, isAbsolute, extname, resolve } from "node:path";

const ROOT = resolve(import.meta.dirname, "..");
const PORT = Number(process.argv[2] || process.env.PORT || 8099);
const MIME = {
  ".js": "text/javascript",
  ".mjs": "text/javascript",
  ".html": "text/html",
  ".css": "text/css",
  ".json": "application/json",
};

createServer(async (req, res) => {
  try {
    let reqPath = decodeURIComponent((req.url || "/").split("?")[0]);
    if (reqPath === "/") reqPath = "/popup.html";
    // join() normalises, collapsing any decoded `..` segments; the served
    // path is then contained iff its path RELATIVE to ROOT neither climbs out
    // (leading `..`) nor is absolute (a different drive/root). Rejecting on
    // that relative form is the traversal barrier CodeQL recognises.
    const full = join(ROOT, reqPath);
    const rel = relative(ROOT, full);
    if (rel.startsWith("..") || isAbsolute(rel)) {
      res.writeHead(403).end("forbidden");
      return;
    }
    const body = await readFile(full);
    res.writeHead(200, { "content-type": MIME[extname(full)] || "application/octet-stream" });
    res.end(body);
  } catch {
    res.writeHead(404).end("not found");
  }
}).listen(PORT, () => console.log(`serving ${ROOT} on http://127.0.0.1:${PORT}`));
