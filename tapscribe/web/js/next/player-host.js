// @ts-check
// The Player's ONE binding to the shell's DOM.
//
// `components/player.js` is the policy (DOM-free, injected media element);
// this is the single place that hands it the real `<audio>` from next.html and
// paints the docked bar around it. Views never touch either: they receive the
// player through their build ctx and call `load` / `seek` / `forget`.
//
// The bar is mutated IN PLACE (hidden toggled, two text cells written), never
// swapped, and only ever from the player's own onChange — so it carries no
// render signature, takes no interaction hold, and the poll can't reach it.
// See CONTEXT.md "Player · seek target · open WAV · playhead" and ADR-0017.

import { createPlayer } from "./components/player.js";
import { truncMid } from "../formatters.js";

/** Why the Player let go of a file, in the operator's words. */
/** @type {Record<string, string>} */
const REASON_TEXT = Object.freeze({
  deleted: "recording deleted",
  unreadable: "recording can't be read",
});

/**
 * Bind the shell's audio element + docked bar. Called once at boot.
 * @param {{ bar: HTMLElement, media: any, name: HTMLElement, msg: HTMLElement }} nodes
 */
export function createPlayerHost({ bar, media, name, msg }) {
  const player = createPlayer({
    media,
    onChange: (loaded, reason) => {
      // The bar outlives the file when there's something to say: an unload the
      // operator didn't ask for (a delete, an unreadable WAV) has to explain
      // itself, and this bar is the only place that can. It hides once a
      // subsequent load clears the message.
      // NB: `hidden` is load-bearing beyond visibility — next.css keys
      // `--next-player-h` off it with :has(), so the shell gives up exactly the
      // bar's height instead of being overlaid by it. No class to keep in sync.
      bar.hidden = !loaded && !reason;
      name.textContent = loaded ? truncMid(loaded.name, 44) : "";
      msg.textContent = reason ? REASON_TEXT[reason] || reason : "";
    },
  });
  return player;
}
