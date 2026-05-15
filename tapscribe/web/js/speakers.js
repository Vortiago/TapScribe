// Stable per-speaker palette index. First five distinct speakers seen in a
// session get indices 0..4; subsequent speakers wrap. The map persists for
// the lifetime of the page so a speaker keeps the same colour across renders.

const idxMap = new Map();
let next = 0;

export function speakerIndex(name) {
  if (!name) return 0;
  if (!idxMap.has(name)) {
    idxMap.set(name, next % 5);
    next++;
  }
  return idxMap.get(name);
}

// Resolve aliases[rawSpeaker] → display string. Falls back to raw name.
export const aliasOf = (speaker, aliases) =>
  (speaker && aliases?.[speaker]) || speaker || "";
