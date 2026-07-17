// @ts-check
// Adaptive cadence for the /api/state poll loop (issue #247, ADR-0013).
//
// The dashboard polls every FAST_MS. That's the right cadence while anything is
// moving, but a dashboard left open on an idle meeting fires a 304 twice a
// second forever. This pacer backs the interval off to SLOW_MS once the poll
// has been idle-AND-unchanged for IDLE_STREAK consecutive ticks with no job/tap
// active, and snaps straight back to FAST_MS on the first real change or
// operator interaction (`wake()`). Pure + timer-free so the rule is unit-tested
// without wall-clock (poll-pacer.test.js); main.js owns the actual setTimeout.

export const FAST_MS = 500;
export const SLOW_MS = 2000;
export const IDLE_STREAK = 4;

/**
 * @typedef {object} PollSignal
 * @property {boolean} changed  This poll returned real (non-304) state.
 * @property {boolean} active   A job or tap is currently in-flight.
 */

/**
 * @param {{ fast?: number, slow?: number, streak?: number }} [opts]
 */
export function createPollPacer({ fast = FAST_MS, slow = SLOW_MS, streak = IDLE_STREAK } = {}) {
  let quiet = 0;
  return {
    /**
     * Record the outcome of a poll and return the delay (ms) before the next.
     * @param {PollSignal} signal
     * @returns {number}
     */
    record({ changed, active }) {
      if (changed || active) {
        quiet = 0;
        return fast;
      }
      quiet += 1;
      return quiet >= streak ? slow : fast;
    },
    /** Operator interaction (or tab re-show): force the next poll back to fast. */
    wake() {
      quiet = 0;
    },
  };
}
