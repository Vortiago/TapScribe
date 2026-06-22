// Single source of truth for the prototype gallery (../index.html) AND the
// floating launcher (./launcher.js). Add a prototype here and both update.
// Hrefs are relative to the prototypes/ root; the launcher absolutizes them
// against its own location so they work from any depth.

export const PROTOTYPES = [
  {
    id: "setup",
    title: "Setup / install",
    tag: "new",
    blurb:
      "Three first-run setup experiences that replace the start.sh → install_picker.py " +
      "terminal flow. Browser-based, live progress, copyable secrets.",
    home: "setup/",
    links: [
      { label: "D · Centered console", href: "setup/?variant=D" },
      { label: "C · Provision", href: "setup/?variant=C" },
      { label: "A · One Tap", href: "setup/?variant=A" },
      { label: "B · Setup Assistant", href: "setup/?variant=B" },
    ],
  },
  {
    id: "stages",
    title: "Stages dashboard",
    blurb:
      "Operator-grade control surface: a two-group spine (Global · This session) plus " +
      "one dense workspace. Where the main dashboard UX could go.",
    home: "stages/",
    links: [{ label: "Open dashboard", href: "stages/" }],
  },
];

/** Which prototype is the current page in, by URL? null on the gallery itself. */
export function currentPrototypeId(pathname = location.pathname) {
  return PROTOTYPES.find((p) => pathname.includes(`/${p.id}/`))?.id ?? null;
}
