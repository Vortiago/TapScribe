// Unit tests for the shared family-grouped model <select> builder (#225,
// run via `node --test`, no DOM). groupModelsByFamily is the pure half of
// model-select.js (bucket + order models by family) — buildModelSelect's DOM
// half has no unit coverage here by design, same rationale as live-feed.js:
// it's covered by the playwright dashboard e2e (test_dashboard_ui.py), which
// exercises the real rendered Settings live-model row and live-channel panel.

import { test } from "node:test";
import assert from "node:assert/strict";

import { FAMILY_LABELS, LIVE_FAMILY_LABELS, groupModelsByFamily } from "./model-select.js";

/** @param {string} model_id @param {string} family */
const m = (model_id, family) => ({ model_id, family, display_name: model_id });

test("groupModelsByFamily orders groups per familyLabels order, skipping absent families", () => {
  const models = [m("voxtral-mini", "voxtral"), m("tiny.en", "whisper")];
  const groups = groupModelsByFamily(models, FAMILY_LABELS);
  assert.deepEqual(
    groups.map((g) => g.label),
    ["Whisper", "Voxtral (Mistral)"],
  ); // nb-whisper/parakeet absent from input → no empty groups
  assert.deepEqual(groups[0].models, [m("tiny.en", "whisper")]);
  assert.deepEqual(groups[1].models, [m("voxtral-mini", "voxtral")]);
});

test("groupModelsByFamily keeps model order within a family", () => {
  const models = [m("a", "whisper"), m("b", "whisper"), m("c", "whisper")];
  const groups = groupModelsByFamily(models, FAMILY_LABELS);
  assert.deepEqual(
    groups[0].models.map((x) => x.model_id),
    ["a", "b", "c"],
  );
});

test("groupModelsByFamily spills families absent from familyLabels into one trailing Other group", () => {
  const models = [m("tiny.en", "whisper"), m("mystery-1", "canary"), m("mystery-2", "byzantine")];
  const groups = groupModelsByFamily(models, FAMILY_LABELS);
  assert.deepEqual(
    groups.map((g) => g.label),
    ["Whisper", "Other"],
  );
  assert.deepEqual(
    groups[1].models.map((x) => x.model_id),
    ["mystery-1", "mystery-2"],
  ); // both unknown families collapse into the SAME Other group
});

test("groupModelsByFamily returns an empty array for no models", () => {
  assert.deepEqual(groupModelsByFamily([], FAMILY_LABELS), []);
});

test("groupModelsByFamily treats a missing family as unknown (Other)", () => {
  const groups = groupModelsByFamily([{ model_id: "x", display_name: "x" }], FAMILY_LABELS);
  assert.deepEqual(groups, [{ label: "Other", models: [{ model_id: "x", display_name: "x" }] }]);
});

// --- LIVE_FAMILY_LABELS: pin the "one table, sliced" contract so a family
// added to FAMILY_LABELS doesn't silently drift the live subset's order.
//
// Asserted against LITERALS, not against FAMILY_LABELS: LIVE_FAMILY_LABELS IS
// `FAMILY_LABELS.filter(...)`, so any assertion derived from FAMILY_LABELS
// compares the same tuple references to themselves and passes by construction —
// it would stay green if every label were changed to the wrong text, or if
// parakeet were quietly made live-eligible. The expected list below is the
// independent source: what the dashboard's live-model dropdowns must offer.

test("LIVE_FAMILY_LABELS is the live-eligible slice, in dropdown order", () => {
  assert.deepEqual(
    LIVE_FAMILY_LABELS.map(([fam, label]) => [fam, label]),
    [
      ["whisper", "Whisper"],
      ["nb-whisper", "NB-Whisper (Norwegian)"],
      ["voxtral", "Voxtral (Mistral)"],
      ["moonshine", "Moonshine"],
    ],
  );
});

test("parakeet is offered for batch but NOT for live", () => {
  assert.ok(
    FAMILY_LABELS.some(([fam]) => fam === "parakeet"),
    "parakeet is a batch-eligible family",
  );
  assert.ok(
    !LIVE_FAMILY_LABELS.some(([fam]) => fam === "parakeet"),
    "parakeet has no live-eligible model today — adding one must be a deliberate edit here",
  );
});
