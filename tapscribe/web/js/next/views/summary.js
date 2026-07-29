// @ts-check
// gate-allow: signal-listener — handlers attach to nodes this view builds and owns; an evicted or rebuilt view drops the whole subtree with its listeners (no document/window targets here). Revisit if views gain a mount AbortSignal.
// Stages · Summary (SESSION stage 4) — the post-transcription summarizer.
//
// WIRED for the Local, Command, and API sources. Local (#86) is the bundled,
// offline, hardware-routed model and the DEFAULT source here (view-local
// default until #84 makes it operator-configurable) — it carries a model
// picker, populated once from the hardware-routed catalog
// (GET /api/summarize/models), so the operator can A/B local models. Command
// (#82) takes a CLI template (e.g. `claude -p`). API (#85) targets an
// OpenAI-compatible / Ollama chat-completions endpoint (base_url + model + a
// write-only key). The operator picks a source,
// edits the prompt, and clicks Generate; we POST to
// /api/sessions/{session}/summarize, which summarizes the session's merged
// transcript and returns the result. We render the summary + the source/model
// (or command) that produced it, surface errors, and drive the shared
// job-progress bar while the job runs.
//
// Persistence (#83): a generated summary is stored server-side next to the
// merged transcript; the session row carries a slim `session_summary` marker
// and this view lazily fetches the body (cached per (session, summarized_at))
// so a stored summary re-renders on revisit/reload without re-generating. No
// saved config yet (#84): the source/command/prompt are entered here and sent
// per Generate.
//
// Interaction hold: the source buttons, command <input>, and prompt <textarea>
// are built ONCE from the template and NEVER rebuilt per-tick (update() only
// mutates the button/note/job-bar/header + the output pane; the source selector
// is click-driven), so a background poll can't clobber a mid-edit prompt — the
// dashboard interaction-hold sweep covers them. The output pane re-render is
// selection-guarded (the summary is a copy target, like the merged-transcript
// pane).

import { tpl, pick, renderRegion, markRegionStale, renderMarkdown } from "../../templates.js";
import { createEmptyStateSync } from "../../vc/components/empty-state/empty-state.js";
import { postJson, putJson, sessionSummary, errText } from "../../api.js";
import { wireSave } from "../../save-status.js";
import { wireSummarizerControls } from "../components/summarizer-controls.js";
import { header, strong, inline, renderJobBar, sessionLabel } from "../shell.js";
import { makeStatusFlasher, copyToClipboard, downloadFile, showTextForManualCopy } from "../ui.js";

/**
 * @param {{
 *   afterMutate: () => void,
 * }} ctx
 * @returns {{ node: DocumentFragment, update: (j: import('../../types.js').AppState, session: import('../../types.js').Session | null) => void }}
 */
export function build(ctx) {
  const { afterMutate } = ctx;
  const frag = tpl("tpl-next-view-summary");

  const headHost = pick(frag, "head");
  const sumOut = pick(frag, "sumOut");
  const sumOutHint = pick(frag, "sumOutHint");
  const cmdInput = /** @type {HTMLInputElement} */ (pick(frag, "sumCmd"));
  const cmdPresetSel = /** @type {HTMLSelectElement} */ (pick(frag, "sumCmdPreset"));
  const cmdPresetNote = pick(frag, "sumCmdPresetNote");
  const cmdPreview = pick(frag, "sumCmdPreview");
  const promptTa = /** @type {HTMLTextAreaElement} */ (pick(frag, "sumPrompt"));
  const saveSessBtn = /** @type {HTMLButtonElement} */ (pick(frag, "sumSaveSession"));
  const useDefaultBtn = /** @type {HTMLButtonElement} */ (pick(frag, "sumUseDefault"));
  const saveStatus = pick(frag, "sumSaveStatus");
  const overrideNote = pick(frag, "sumOverrideNote");
  const genBtn = /** @type {HTMLButtonElement} */ (pick(frag, "sumGenerate"));
  const sumNote = pick(frag, "sumNote");
  const jobBar = pick(frag, "jobBar");
  const jobLabel = pick(frag, "jobLabel");
  const jobCount = pick(frag, "jobCount");
  const jobProgress = /** @type {HTMLElement} */ (pick(frag, "jobProgress"));
  const jobWav = pick(frag, "jobWav");
  const sumCopyStatus = pick(frag, "sumCopyStatus");
  const sumCopyBtn = /** @type {HTMLButtonElement} */ (pick(frag, "sumCopyBtn"));
  const sumDownloadMd = /** @type {HTMLButtonElement} */ (pick(frag, "sumDownloadMd"));

  // Source selector + per-source detail panes + the local model picker —
  // all built ONCE from the template and handed to the shared
  // summarizer-controls component below (never a per-tick rebuild, so the
  // interaction hold holds; the command <input> / prompt <textarea> live
  // inside, untouched).
  const srcButtons = /** @type {NodeListOf<HTMLButtonElement>} */ (
    frag.querySelectorAll(".segctl--wide [data-src]")
  );
  const srcLocal = /** @type {HTMLElement} */ (pick(frag, "srcLocal"));
  const srcCommand = /** @type {HTMLElement} */ (pick(frag, "srcCommand"));
  const srcApi = /** @type {HTMLElement} */ (pick(frag, "srcApi"));
  const modelSel = /** @type {HTMLSelectElement} */ (pick(frag, "sumModel"));
  const modelNote = pick(frag, "sumModelNote");
  const maxTokInput = /** @type {HTMLInputElement} */ (pick(frag, "sumMaxTokens"));
  const apiBaseInput = /** @type {HTMLInputElement} */ (pick(frag, "sumApiBase"));
  const apiModelInput = /** @type {HTMLInputElement} */ (pick(frag, "sumApiModel"));
  const apiKeyInput = /** @type {HTMLInputElement} */ (pick(frag, "sumApiKey"));
  const apiKeyNote = pick(frag, "sumApiKeyNote");

  // ---- View-local state -----------------------------------------------------
  /** @type {import('../../types.js').Session | null} */
  let session = null;
  /** The body THIS tab just generated (null until a Generate lands) — shown
   * during the window before /api/state's marker catches up, and superseded as
   * soon as that marker carries a NEWER `summarized_at` (see update()). */
  /** @type {import('../../types.js').PersistedSummary | null} */
  let lastSummary = null;
  /** The session id `lastSummary` belongs to — so switching sessions clears a
   * summary that isn't this session's. */
  let summarySession = "";
  /** True while a Generate POST is in flight (the button reflects it). */
  let generating = false;
  /** Sticky error message from the last failed Generate — shown in the note
   * until the next Generate clears it. */
  let errorMsg = "";
  /** The template's baked-in prompt — the view-side bottom of the effective
   * chain (matches the server's DEFAULT_SUMMARY_PROMPT). Captured before any
   * seed touches the textarea. */
  const builtinPrompt = promptTa.value;
  /** One-shot: the GLOBAL-layer fields (command/model/max_tokens) seed from
   * the first poll carrying `summarizer_default` and are then left alone —
   * unlike source/prompt they don't re-seed per session (they have no
   * per-session override), so a hand-edit survives session switches. */
  let globalSeeded = false;
  /** The last-seen global default (mirrored each tick) — the reset button
   * seeds from it DIRECTLY rather than waiting a poll, because afterMutate's
   * synchronous re-render still carries the stale (pre-clear) session-meta. */
  /** @type {import('../../types.js').SummarizerDefault | null} */
  let lastDefault = null;
  /** Captured summary text and session id for copy/download handlers. */
  let copySummaryText = /** @type {string | null} */ (null);
  let copySummarySid = "";

  // The controls' render gate. The other two regions own theirs: the output pane
  // renders through `renderRegion(sumOut, …, {sig})`, and the header through
  // header()'s own `headerNeedsRender` (via the lazy `{sig, build}` sub-line).
  let lastCtlSig = " ";

  // ---- Helpers --------------------------------------------------------------

  const hasTranscript = () => !!session?.session_transcript;

  /** This view's watcher on the persisted summary: when one lands, force the
   * output pane past its sig gate and render now. */
  const storedBody = sessionSummary.watch(() => { markRegionStale(sumOut); afterMutate(); });

  /**
   * The persisted summary behind the session's slim marker, from the lazy
   * resource keyed by (session, summarized_at). The resolve owns fetch-once, the
   * retry pacing, and the per-session last-good hold that keeps an EXTERNAL
   * re-summarize (the end-of-meeting pipeline, a second tab) from dropping the
   * output pane to the "No summary yet" empty state for a whole round trip
   * (#266). null only on a genuine cold load — the placeholder shows meanwhile.
   * @param {import('../../types.js').SummaryMarker | null | undefined} marker
   * @param {string} sid
   * @returns {import('../../types.js').PersistedSummary | null}
   */
  const resolveStored = (marker, sid) => {
    if (!marker || !marker.summarized_at || !sid) return null;
    return storedBody.resolve([sid, marker.summarized_at]).value;
  };

  /** Sync the Generate button + the note line from current state. Never touches
   * the command/prompt inputs (the operator owns those). */
  const reflectControls = () => {
    saveSessBtn.disabled = !session || generating;
    useDefaultBtn.disabled = !session || generating;
    genBtn.disabled = !session || generating;
    genBtn.textContent = generating ? "⟳ Generating…" : "🪄 Generate summary";
    if (errorMsg) {
      sumNote.textContent = errorMsg;
      sumNote.classList.add("is-err");
      return;
    }
    sumNote.classList.remove("is-err");
    sumNote.textContent = generating
      ? ""
      : !session
        ? "pick a session from the spine first"
        : !hasTranscript()
          ? "transcribe this session first — Generate needs a merged transcript"
          : "";
  };

  const flashSumCopyStatus = makeStatusFlasher(sumCopyStatus);

  /**
   * @param {import('../../types.js').PersistedSummary} res
   * @param {boolean} [stale] true when the summary predates the current transcript (#94)
   */
  const renderSummary = (res, stale = false) => {
    const out = document.createElement("div");
    out.className = "sumout";
    if (stale) {
      const warn = document.createElement("div");
      warn.className = "sumout__stale";
      warn.textContent =
        "This summary predates the current transcript — regenerate to refresh it.";
      out.append(warn);
    }
    const title = document.createElement("div");
    title.className = "sumout__title";
    title.textContent = "Summary";
    const body = document.createElement("div");
    body.className = "sumtext";
    // The summary is markdown from an UNTRUSTED model — render through
    // renderMarkdown (createElement/textContent only, injection-proof by
    // construction), never innerHTML.
    body.append(renderMarkdown(res.summary || ""));
    out.append(title, body);
    // Show what produced it: the model (local/api) if present, else the command
    // template (command source), else just the source name. Side-effect inside
    // the build closure so it only fires on a real render (renderRegion gate).
    sumOutHint.textContent = res.model
      ? `${res.source} · ${res.model}`
      : res.command
        ? `${res.source} · ${res.command}`
        : res.source;
    return out;
  };

  /** @param {import('../../types.js').Session | null} sess */
  const renderPlaceholder = (sess) => {
    // vc empty-state (js/vc, warmed at boot) — .work .empty-state in next.css
    // keeps the old .empty metrics so the pane looks unchanged.
    const empty = createEmptyStateSync({
      title: sess ? "No summary yet" : "No session selected",
      detail: sess
        ? hasTranscript()
          ? "Edit the prompt and click Generate to summarize this session's merged transcript."
          : "Transcribe this session first, then Generate a summary from its merged transcript."
        : "Pick a session from the spine to summarize it.",
    }).el;
    sumOutHint.textContent = "";
    return empty;
  };

  // ---- Generate (REAL) ------------------------------------------------------
  // Bound ONCE at build time; reads the live input values at click time.
  genBtn.addEventListener("click", async () => {
    if (!session || generating) return;
    const sid = session.session;
    errorMsg = "";
    generating = true;
    lastCtlSig = " "; // re-sync the button immediately, don't wait for a poll
    reflectControls();
    try {
      // The Local source carries the picked model + output cap (empty/null →
      // server defaults); the Command source carries a CLI template instead.
      const v = ctl.values();
      /** @type {{ source: string, prompt: string, command?: string, model?: string, max_tokens?: number, base_url?: string, api_key?: string }} */
      const body = { source: v.source, prompt: promptTa.value };
      if (v.source === "command") body.command = v.command;
      if (v.source === "api") {
        // base_url in the clear; the key only when the operator typed one this
        // Generate (blank → fall back to the stored default's key).
        body.base_url = v.base_url;
        if (v.api_key) body.api_key = v.api_key;
      }
      // model + max_tokens are shared by the local + api sources (the command
      // source carries neither); empty/null fall back to the server defaults.
      if (v.source !== "command") {
        if (v.model) body.model = v.model;
        if (v.max_tokens != null) body.max_tokens = v.max_tokens;
      }
      const res = /** @type {import('../../types.js').SummaryResult} */ (
        await postJson(`/api/sessions/${encodeURIComponent(sid)}/summarize`, body)
      );
      lastSummary = res;
      summarySession = sid;
      markRegionStale(sumOut); // force the output pane to re-render with the new summary
    } catch (e) {
      errorMsg = `failed: ${errText(e)}`;
    } finally {
      generating = false;
      lastCtlSig = " ";
      reflectControls();
      // Re-poll for the authoritative state (the job slot is freed server-side).
      // afterMutate() re-renders synchronously, so a fresh summary lands via the
      // output-pane renderRegion (marked stale on success) — which also honours
      // the copy-selection guard a direct render here would bypass.
      afterMutate();
    }
  });

  sumCopyBtn.addEventListener("click", async () => {
    // SNAPSHOT before the await: a poll tick can re-render the pane (a spine
    // click, or a superseded summary) and clear copySummaryText while the
    // clipboard write is in flight. A fallback that re-read the mutable would
    // then prompt with the literal "null" — hence a local, which also keeps
    // tsc's null-check on this path instead of a cast that hides it.
    const text = copySummaryText;
    if (!text) return;
    await copyToClipboard(text, {
      onOk: () => flashSumCopyStatus("✓ copied"),
      onFallback: () => {
        // A markdown summary is a multi-line DOCUMENT, so it gets the shared
        // new-tab treatment (a prompt() default is a single-line input that
        // flattens it); the prompt is only the popup-blocked degradation.
        const how = showTextForManualCopy(text, "Copy the summary (Ctrl/Cmd-C, Enter, Esc):");
        flashSumCopyStatus(how === "tab" ? "↗ opened in new tab" : "↗ shown in prompt");
      },
    });
  });

  sumDownloadMd.addEventListener("click", () => {
    const text = copySummaryText;
    if (!text || !copySummarySid) { flashSumCopyStatus("nothing to export"); return; }
    downloadFile(text, "summary_" + copySummarySid + ".md", "text/markdown;charset=utf-8");
  });

  // ---- Summarizer controls (shared component) ---------------------------------
  // Source segctl + model/preset/max-tokens wiring is the shared
  // summarizer-controls component (the Settings card uses the same one);
  // this view adds the "will run" preview, which also reads the prompt.

  /** Spell out what Generate will run: the template verbatim + the prompt as
   * a quoted trailing argument (elided past 80 chars), then the stdin note. */
  const reflectCmdPreview = () => {
    const cmd = cmdInput.value.trim();
    if (!cmd) {
      cmdPreview.replaceChildren(); // static-render — input-event reflect of a text-only preview; not a polled region
      return;
    }
    const p = promptTa.value.trim();
    const shown = p.length > 80 ? `${p.slice(0, 77)}…` : p;
    const promptArg = shown ? ` "${shown.replace(/\\/g, "\\\\").replace(/"/g, '\\"')}"` : "";
    const l1 = document.createElement("div");
    l1.textContent = `will run: ${cmd}${promptArg}`;
    const l2 = document.createElement("div");
    l2.textContent = "merged transcript → stdin";
    cmdPreview.replaceChildren(l1, l2); // static-render — same input-event reflect
  };
  promptTa.addEventListener("input", reflectCmdPreview);

  const ctl = wireSummarizerControls({
    buttons: srcButtons,
    srcKey: "src",
    localPane: srcLocal,
    commandPane: srcCommand,
    apiPane: srcApi,
    modelSel,
    modelNote,
    emptyModelNote: "first Generate downloads the model, then it runs fully offline",
    maxTokInput,
    presetSel: cmdPresetSel,
    presetNote: cmdPresetNote,
    cmdInput,
    apiBaseInput,
    apiModelInput,
    apiKeyInput,
    apiKeyNote,
    canSwitch: () => !generating,
    onCommandInput: reflectCmdPreview,
  });
  reflectCmdPreview(); // seed from the template defaults at build

  // ---- Effective config (#84) -----------------------------------------------
  // The controls pre-fill from the EFFECTIVE config: the session-meta override
  // (source + prompt) over the global default over built-ins — the same chain
  // the server resolves for omitted Generate fields and the pipeline. Seeding
  // is keyed strictly on a session-id CHANGE (the switch block below), so a
  // poll tick can never clobber a mid-edit prompt; Generate still sends the
  // live values, so hand-edits behave exactly as before.

  /** Seed source + prompt from the two effective-config layers — empty falls
   * back, bottoming out on the built-ins. ONE chain, two deliberate call
   * sites: the session-switch block (override over global) and the reset
   * button (global only, meta just cleared). */
  /** @param {{ summary_source?: string, summary_prompt?: string }} meta
   *  @param {Partial<import('../../types.js').SummarizerDefault>} d */
  const seedFromLayers = (meta, d) => {
    ctl.setSource(meta.summary_source || d.source || "local");
    promptTa.value = meta.summary_prompt || d.prompt || builtinPrompt;
    reflectCmdPreview();
  };

  // "save for this session": persist source + prompt as the per-session
  // override (the shared saving…/saved badge lifecycle).
  wireSave({
    btn: saveSessBtn,
    status: saveStatus,
    put: () => {
      if (!session) return Promise.reject(new Error("no session selected"));
      return putJson(`/api/session-meta/${encodeURIComponent(session.session)}`, {
        summary_source: ctl.source,
        summary_prompt: promptTa.value,
      });
    },
    onSuccess: () => afterMutate(),
  });

  // "use global default": clear the override (empty falls back server-side)
  // and re-seed source + prompt from the last-seen global — directly, the
  // ONE deliberate re-seed outside a session switch. (Deliberately NOT via
  // the switch block: afterMutate's synchronous re-render still carries the
  // stale pre-clear session-meta, which would seed the old override back.)
  wireSave({
    btn: useDefaultBtn,
    status: saveStatus,
    put: async () => {
      if (!session) throw new Error("no session selected");
      await putJson(`/api/session-meta/${encodeURIComponent(session.session)}`, {
        summary_source: "",
        summary_prompt: "",
      });
      seedFromLayers({}, lastDefault || {});
    },
    onSuccess: () => afterMutate(),
  });

  // ---- Per-tick update ------------------------------------------------------

  /**
   * @param {import('../../types.js').AppState} j
   * @param {import('../../types.js').Session | null} sess
   */
  const update = (j, sess) => {
    session = sess;
    const sid = sess?.session || "";
    const job = sess?.progress || null;

    // ---- Saved config (#84): the global-layer fields seed once; the
    // override indicator is a derived, non-interactive display bit, so it
    // updates in place every tick (the #94 sibling-toggle pattern) — never
    // through a sig-gated pane.
    if (j.summarizer_default) lastDefault = j.summarizer_default;
    if (!globalSeeded && j.summarizer_default) {
      globalSeeded = true;
      ctl.seedSaved(j.summarizer_default);
    }
    const meta = sess?.session_meta || {};
    overrideNote.textContent =
      meta.summary_source || meta.summary_prompt ? "session override active" : "";

    // ---- Job progress — in-place writes on prebuilt nodes, EVERY tick
    // (deliberately outside the signature gates, same as transcript.js). Scoped
    // to a summarize job so a transcribe/strip on the same session doesn't show
    // in the Summarizer panel.
    renderJobBar({ jobBar, jobLabel, jobCount, jobProgress, jobWav }, job, { only: "summarize" });

    // ---- Header — gated by header()'s OWN headerNeedsRender, through the lazy
    // `{sig, build}` sub-line (capture.js / people.js shape): pairing the sig
    // WITH the builder is what makes forgetting a term impossible. The sig
    // mirrors the FULL rendered text, so it can't collide with the sess-null
    // fallback string below.
    header(headHost, {
      eyebrow: "Session · 4 Summary",
      title: "Summary",
      sub: sess
        ? {
            sig: `summarize ${sessionLabel(sess)}`,
            build: () => inline("summarize ", strong(sessionLabel(sess))),
          }
        : "no session selected — pick one from the spine",
    });

    // ---- Session switch — drop a summary/error that belonged to another
    // session, force the output + controls to re-sync, and pre-fill source +
    // prompt from the session's EFFECTIVE config (#84). The id is recorded
    // FIRST, so re-seeding requires a different session — a later tick can't
    // clobber a mid-edit prompt.
    if (sid !== summarySession) {
      summarySession = sid;
      lastSummary = null;
      errorMsg = "";
      markRegionStale(sumOut);
      lastCtlSig = " ";
      if (sess) seedFromLayers(sess.session_meta || {}, j.summarizer_default || {});
    }

    // ---- Output pane — rendered through renderRegion: it gates on outSig AND
    // defers (without advancing) while a text selection is active inside it, so
    // a tick can't dissolve a mid-copy selection. The hint side-effect lives in
    // the build closures, so it only fires on a real render.
    // A persisted summary (slim marker on the session) is resolved lazily — the
    // cached body when in hand, else this session's last-good body + a one-shot
    // fetch that marks the pane stale when it lands (resolveStored).
    // The just-POSTed body (`lastSummary`) wins only until the STORED marker
    // moves past it: a summary regenerated elsewhere — the end-of-meeting
    // pipeline (processSession) or a second tab — carries a newer
    // `summarized_at`, and holding the local body then showed the operator a
    // superseded summary forever (lastSummary is cleared only on a session
    // switch, so no later tick could ever displace it) with no cue, since the
    // #94 staleness banner only fires when the TRANSCRIPT also moved. While the
    // newer body is still in flight, keep showing the local one rather than
    // blanking the pane.
    const marker = sess?.session_summary || null;
    const localStamp = lastSummary?.summarized_at || "";
    const markerStamp = marker?.summarized_at || "";
    const superseded = !!markerStamp && markerStamp > localStamp;
    const stored = superseded || !lastSummary ? resolveStored(marker, sid) : null;
    const shown = superseded ? stored || lastSummary : lastSummary || stored;
    // Staleness (#94): the summary carries the `transcribed_at` of the transcript
    // it was built from; if the session was re-transcribed since, the live
    // transcript marker's stamp is newer. Prefer the resolved body's stamp, fall
    // back to the slim marker's. ISO 8601 from one source → lexical compare is
    // chronological. `stale` joins outSig so a re-transcribe re-renders the pane.
    const curStamp = sess?.session_transcript?.transcribed_at || "";
    const sumStamp = shown?.transcribed_at || marker?.transcribed_at || "";
    const stale = !!(curStamp && sumStamp && sumStamp < curStamp);
    const outSig = [
      sid,
      // The body actually rendered — NOT `lastSummary`'s stamp, which no longer
      // decides what `shown` is (see the supersede rule above).
      shown?.created_at || "",
      marker?.summarized_at || "",
      shown ? 1 : 0,
      hasTranscript() ? 1 : 0,
      stale ? 1 : 0,
    ].join("§");
    const renderSummaryOut = () => {
      // ONE predicate behind both controls and both handlers: what's ENABLED is
      // exactly what's exportable. A persisted-but-empty `summary` would
      // otherwise light up two buttons whose clicks do nothing.
      const text = shown?.summary || "";
      copySummaryText = text || null;
      copySummarySid = text ? sid : "";
      sumCopyBtn.disabled = !text;
      sumDownloadMd.disabled = !text;
      return shown ? renderSummary(shown, stale) : renderPlaceholder(sess);
    };
    renderRegion(sumOut, renderSummaryOut, {
      sig: outSig,
    });

    // ---- Controls (button + note) — gated. The command/prompt inputs are NOT
    // rebuilt here (interaction hold).
    const ctlSig = [sid, hasTranscript() ? 1 : 0, generating ? 1 : 0, errorMsg ? 1 : 0].join("§");
    if (ctlSig !== lastCtlSig) {
      lastCtlSig = ctlSig;
      reflectControls();
    }
  };

  return { node: frag, update };
}
