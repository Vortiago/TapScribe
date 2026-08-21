// Hand-written TypeScript declarations for tapscribe/web/js/.
// Reference from JSDoc (no runtime import):
//   /** @type {import('./types.js').AppState} */
//
// Source ground-truth:
//   tapscribe/routes/state.py  — /api/state (payload: tapscribe/state_view.py)
//   tapscribe/routes/*.py      — the mutation routes, one module per resource group
//   tapscribe/recorder.py      — ActiveStream, JobState dataclasses
//   tapscribe/session_merge.py — MergedTranscript, Segment shapes
//   tapscribe/sessions.py      — gather_sessions() per-session shape
//   tapscribe/web/js/main.js   — ctx prop-bags passed to render()

// The language→specialist-model map, registry-filtered server-side to the rows
// that will actually run (`transcribers.catalog.effective_specialists`). One
// type for the two readouts that carry it — /api/languages and /api/state.
export type SpecialistMap = Record<string, string>;

// ---------------------------------------------------------------------------
// /api/state response
// ---------------------------------------------------------------------------

export interface AppState {
  current_session: string;
  active: ActiveStream[];
  sessions: Session[];
  default_override_counts: { prompt: number; hotwords: number; summarizer: number };
  live_feed: LiveFeedEntry[];
  live_info: LiveInfo;
  live_log: string[];
  live_supports_native_vad: boolean;
  backend: string;
  available_backends: string[];
  recording_enabled: boolean;
  prompt: ConfigFile;
  live_prompt: ConfigFile;
  // Operator's saved DEFAULT live-channel model id (live-model.txt); "" when
  // unset. Distinct from live_info.model (what's actually running) — the Live
  // engine card flags "restart to apply" while they differ.
  live_model_default: string;
  // Operator's saved DEFAULT batch model id (batch-model.txt); "" when unset.
  // Seeds Settings' Default engine selector and resolves the end-of-meeting
  // pipeline's transcribe stage server-side.
  batch_model_default: string;
  // The generalist that will ACTUALLY run — batch_model_default validated against
  // the catalog + defaulted when unset/invalid (ADR-0011). The Transcript readout
  // names this so it can't show a model the transcribe won't use.
  batch_model_effective: string;
  hotwords: ConfigFile;
  inputs_support: InputsSupport;
  // The structured global summarizer default (#84) — the NON-SECRET projection
  // of config/summarizer.json (summarizer_default_public server-side; #85's
  // API-key fields must never appear here). "" / null = unset (built-ins
  // apply). Seeds the Settings card and the Summary view's effective config.
  summarizer_default: SummarizerDefault;
  // The operator's DEFAULT candidate-language set (ADR-0010). The full
  // selectable catalog is served once via GET /api/languages; this carries only
  // the small current value the picker pre-selects.
  languages: { path: string; default: string[] };
  hallucinations: HallucinationsConfig;
  // The operator knobs (#210), each the value IN FORCE — resolved env > config
  // file > default, and for the overlap after the joint chunk/overlap clamp — so
  // the Settings card renders what the recorder actually uses, not what is
  // stored. Written back through PUT /api/config/{key}.
  idle_ttl_s: number; // seconds; -1 = never evict
  parakeet_chunk_s: number;
  parakeet_overlap_s: number;
  summarize_timeout_s: number;
  summarize_gguf_ctx: number;
  // Specialist language→model map (read-only, launch-time) — surfaced on /api/state.
  specialists: SpecialistMap;
  // The cross-session People Registry view (ADR-0009): one row per canonical
  // Person, aggregated server-side from every session's roster + the live
  // identities. The People view renders these directly; rename/merge/detach
  // mutate via /api/people.
  people: Person[];
}

// One canonical Person row in the cross-session registry (server-built by
// name_resolution.build_people_view). `name` is the operator-chosen name, or
// the bridge/roster default when `named` is false. `identities` are the
// bridge-stamped device tokens this Person owns (>1 only after a merge).
export interface Person {
  id: string;                 // opaque server-generated person id ("p_<hex>")
  name: string;               // display name (chosen if `named`, else default)
  named: boolean;             // operator has explicitly named this Person
  identities: string[];       // member device identities (the join key)
  sessions: string[];         // session ids this Person appears in
  session_count: number;
  recorded: boolean;          // has at least one recorded occurrence
  live: boolean;              // an identity is currently streaming (active)
}

// The operator's global summarizer default — GET/PUT /api/summarize/config
// and AppState.summarizer_default share this shape.
export interface SummarizerDefault {
  source: string;            // "" (unset → built-in "local") | "local" | "command" | "api"
  prompt: string;
  command: string;           // command source: the CLI template
  model: string;             // local source: catalog repo id ("" = catalog default); api source: remote model name
  max_tokens: number | null; // local/api source: output cap (null = env default)
  base_url: string;          // api source: OpenAI-compatible base URL (Ollama /v1, etc.)
  key_set: boolean;          // api source: whether an api_key is stored — the key itself is NEVER serialised, only this boolean
}

// Active /tap WebSocket (one per recording utterance). `record` and `live`
// are overwritten at serialisation time with the per-identity TapSetting
// preference (tapscribe/state_view.py's `active_rows`), not the WS-open snapshot.
export interface ActiveStream {
  conn_id: string;
  identity: string;
  name: string;
  filename: string;
  started_at: string;           // ISO 8601
  bytes_received: number;
  record: boolean;
  live: boolean;
  level: number;                // 0.0–1.0 volume meter
  lag_s: number | null;         // WlK transcription backlog in seconds
  buffer_transcription: string; // in-flight (uncommitted) hypothesis
  gate_open: boolean;
  session: string | null;       // session this tap was writing to at open time
  // EFFECTIVE single/multi-person mode (ADR-0021): operator override >
  // the Bridge's declaration on the wire > single. Only a multi-person tap is
  // diarized. Absent on a payload from before the feature.
  mode?: "single" | "multi";
}

export interface LiveFeedEntry {
  ts: string;       // ISO 8601
  identity: string;
  name: string;
  text: string;
  session: string;
}

// All values are strings even when the underlying concept is numeric
// (port, pid, gate_*). Empty string = field is unset/not applicable.
export interface LiveInfo {
  state: "stopped" | "starting" | "running" | "error";
  model: string;
  backend: string;
  device: string;
  language: string;
  host: string;
  port: string;
  last_error: string;
  pid: string;
  started_at: string;
  gate_kind: "tapscribe" | "backend" | "";
  gate_speech_threshold: string;
  gate_hangover_ms: string;
  gate_pre_roll_ms: string;
  gate_min_speech_ms: string;
  confidence_validation: "on" | "off" | "";
}

export interface ConfigFile {
  path: string;
  content: string;
  length: number;
}

export interface InputsSupport {
  live_prompt: boolean;
  batch_prompt: boolean;
  batch_hotwords: boolean;
}

export interface HallucinationsConfig {
  path: string;
  content: string;
  count: number;
}

// ---------------------------------------------------------------------------
// Session list (gather_sessions output)
// ---------------------------------------------------------------------------

export interface Session {
  session: string;
  wav_count: number;
  // The per-WAV array is NOT on /api/state anymore (a huge session shipped +
  // re-parsed O(WAVs) every poll tick). Fetch it lazily via
  // sessionFiles.fetch(session, files_sig) — cached client-side, refetched only
  // when files_sig changes. Aggregates the listing views need are precomputed:
  total_bytes: number;        // Σ original WAV sizes — sessions.js total size
  total_duration_s: number;   // Σ original WAV durations — spine.js card
  speakers: string[];         // distinct recorded speaker slugs — People view
  // Deterministic digest of the file listing; flips on any add/remove/
  // re-record/transcribe/strip change. The lazy-files cache key + invalidation
  // signal. "" when the session has no folder on disk yet (→ empty list, no
  // fetch).
  files_sig: string;
  is_current: boolean;
  earliest_iso: string | null;
  latest_iso: string | null;
  // SLIM marker only — /api/state no longer embeds the full merged
  // transcript. The full body (segments[]/plain_text/suppressed[]) is fetched
  // lazily via sessionTranscript.fetch(session, transcribed_at), cached
  // client-side. A marker change (new transcribed_at) is the re-fetch signal.
  session_transcript: MergedTranscriptMarker | null;
  // SLIM marker only — the full persisted summary is fetched lazily via
  // sessionSummary.fetch(session, summarized_at). A marker change (new
  // summarized_at) is the re-fetch signal. Null when never summarized.
  session_summary: SummaryMarker | null;
  progress: JobStateSnapshot | null;
  session_meta: SessionMeta;
  // Server-RESOLVED speaker-name map for this session (ADR-0009): slug →
  // display name, resolved through per-session Override > Person name >
  // bridge/roster default. metaFor() layers this over session_meta.aliases so a
  // global rename propagates to the transcript. Absent/empty for an old
  // rosterless session (which then resolves purely via its retained aliases).
  names?: Record<string, string>;
  // Server-projected stamp over each identity's diarization run_id (ADR-0021).
  // The runs themselves are a join input /api/state consumes and drops; this is
  // the key the Voices panel's lazy body rides, and the only signal that a
  // diarize landed. "" for a session that was never diarized.
  voices_sig?: string;
  stripped: StrippedStats | null;
}

// Optional — all fields absent when no session-meta.json exists.
export interface SessionMeta {
  label?: string;
  prompt?: string;
  hotwords?: string;
  // Per-session summarizer override (#84): source + prompt only (per-source
  // fields stay global). "" = no override → the global default applies.
  summary_source?: string;
  summary_prompt?: string;
  // Per-meeting candidate-language override (ADR-0010). Absent/empty = no
  // override → the global default applies.
  languages?: string[];
  aliases?: Record<string, string>;
  // The operator's Voice->Person map (ADR-0021): `identity#<voice>` to a Person
  // pointer stamped with the diarization run it was made against. A mapping
  // whose stamp no longer matches the sidecar is NOT applied and shows as
  // needing re-mapping. Rides the poll, not the lazy Voices body: it changes on
  // a click, that body only on a re-diarize.
  voices?: Record<string, VoiceMapping>;
}

export interface VoiceMapping {
  person_id: string;
  run_id: string;
}

// GET /api/sessions/{session}/voices — the lazy body behind `voices_sig`.
// Span COUNTS and seconds, never the spans themselves: a long meeting is
// thousands of them and the panel draws one row per Voice.
export interface SessionVoices {
  session: string;
  voices_sig: string;
  identities: VoiceIdentity[];
}

export interface VoiceIdentity {
  identity: string;
  name: string;
  run_id: string;
  voices: VoiceRow[];
}

export interface VoiceRow {
  key: string; // `identity#<label>`
  label: string;
  spans: number;
  seconds: number;
}

export interface StrippedStats {
  count: number;
  speech_seconds: number;
  stripped_at: string | null; // ISO 8601 mtime of stripped/
}

// dataclasses.asdict(JobState) — one in-flight job per session at a time.
export interface JobStateSnapshot {
  session: string;
  kind: "transcribe" | "strip" | "diarize" | "summarize" | "pipeline";
  current: number;
  total: number;
  started_at: string; // ISO 8601
  status: string;
  current_file: string | null;
  model: string | null;
  // Which stage a kind="pipeline" job is in; null for single-stage jobs.
  stage: "strip" | "diarize" | "transcribe" | "summarize" | null;
}

// POST /api/sessions/{session}/summarize response — the persisted summary
// plus the ok/session envelope. For the Command source `command` is the CLI
// template and `model` is empty; the Local source populates `model`.
// Persisted (#83): the same body is stored in session-summary.json and read
// back lazily via GET /api/sessions/{session}/summary.
export interface SummaryResult extends PersistedSummary {
  ok: boolean;
  session: string;
}

// One Command-source preset — a known CLI tool whose template a dropdown pick
// seeds into the (still editable) command field. NOT an allowlist.
export interface CommandPreset {
  key: string;
  label: string;
  template: string;
  note: string;
}

// One selectable local-summarizer model — a row of GET /api/summarize/models,
// shown in the Summary view's model dropdown.
export interface SummaryModel {
  repo_id: string;
  label: string;
  approx_gb: number;
  context_tokens: number;
  note: string;
  is_default: boolean;
}

// GET /api/summarize/models — the hardware-routed local model catalog.
export interface SummaryModelCatalog {
  backend: string;            // "mlx" on Apple Silicon, "gguf" elsewhere
  default: string;            // repo_id of the active default
  models: SummaryModel[];
  max_tokens_default: number; // OUTPUT-length cap default the number input seeds
  max_tokens_min: number;     // lower bound for the number input
  max_tokens_max: number;     // upper bound for the number input
  command_presets: CommandPreset[]; // Command-source presets (ride the same fetch)
}

// GET /api/sessions/{session}/files — the lazy per-session WAV listing the
// poll no longer embeds. Fetched once per files_sig via sessionFiles.fetch.
export interface SessionFiles {
  files: WavFile[];
}

export interface WavFile {
  name: string;
  size: number;
  duration_s: number;
  transcript: WavTranscriptMarker | null;   // SLIM marker of the primary cached transcript
  transcripts: WavTranscriptVariant[];       // compact cache_listing of cached model variants
  wav_start: string | null;           // ISO 8601 from filename
  wav_end: string | null;
  speaker_name: string;
  regions: WavRegion[];               // strip-silence output segments
  // True while a tap is writing this WAV. Its RIFF header is patched only at tap
  // close, so it is NOT playable yet (ADR-0017). Absent on region clips, which a
  // tap never writes.
  open?: boolean;
}

// A stripped-silence region — same shape as WavFile, no sub-regions.
export interface WavRegion {
  name: string;
  size: number;
  duration_s: number;
  transcript: WavTranscriptMarker | null;
  transcripts: WavTranscriptVariant[];
  wav_start: string | null;
  wav_end: string | null;
  speaker_name: string;
}

// SLIM per-WAV transcript marker embedded in /api/state — just the fields a
// listing reads without rendering (has-tx, "took Xms", the set-primary
// compare key). The full body is fetched lazily via wavTranscript.fetch.
export interface WavTranscriptMarker {
  transcribed_at?: string; // ISO 8601
  transcribe_ms?: number;
  model?: string;
  backend?: string;
  source?: "original" | "stripped";
  segment_count?: number;
}

// One row of wav_cache.cache_listing — the compact per-(backend,model)
// variant listing for the cache picker. Not the full transcript.
export interface WavTranscriptVariant {
  backend: string;
  model: string;
  is_primary: boolean;
  transcribe_ms?: number;
  // The entry's transcribe source — set-primary sends it back so the server
  // resolves a stripped clip under <session>/stripped/ instead of 404ing.
  source: "original" | "stripped";
  // cache_listing doesn't emit text today; the cache panel's word-count reads
  // it defensively (falls back to 0).
  text?: string;
}

// Full cached per-WAV transcript (the primary model's result) — the lazy
// wavTranscript.fetch result, rendered by buildExpandTx.
export interface WavTranscript {
  transcribed_at: string; // ISO 8601
  transcribe_ms: number;
  model: string;
  backend: string;
  device: string;
  language: string;
  source: "original" | "stripped";
  text: string;
  suppressed_hallucinations: SuppressedHallucination[];
}

export interface SuppressedHallucination {
  start: number | null;
  end: number | null;
  text: string;
  matched_rule: string;
}

// GET /api/wav/{session}/{name}/peaks — a fixed-size server-computed
// downsample of one WAV for the Recordings waveform. `peaks` are normalised
// [0, 1] amplitudes (one per bin); the wire cost is `bins` floats regardless
// of how long the recording is.
export interface WavePeaks {
  peaks: number[];
  bins: number;
  duration_s: number;
  sample_rate: number;
}

// One kept {start_s, end_s} region of a committed strip-silence cut, in
// seconds relative to the ORIGINAL wav. Spans arrive sorted, non-overlapping.
export interface CutSpan {
  start_s: number;
  end_s: number;
  name?: string;  // committed spans (schema v2) carry their region clip's filename
}

// GET /api/wav/{session}/{name}/strip-meta — the committed (on-disk) cut for
// one original: the spans the last ✂ strip wrote, the knobs it ran with, and
// the run stamp. The whole response is null when the session has no
// stripped/ meta or this wav produced no regions.
export interface WavStripMeta {
  spans: CutSpan[];
  stripped_at: string | null;
  knobs?: StripOpts;
}

// GET /api/wav/{session}/{name}/strip-preview — what ✂ strip WOULD cut at
// the given knobs: the kept spans plus the aggregate stats the live overlay
// and the wave-stats row track while a knob drags (#89).
export interface StripPreview {
  spans: CutSpan[];
  in_seconds: number;
  speech_seconds: number;
  segments: number;
  segments_filtered_below_floor: number;
  silent: boolean;
  rms_dbfs: number;
  reason: string | null;
  detector: string | null;
  knobs: StripOpts;
}

// ---------------------------------------------------------------------------
// Merged (session-level) transcript.
//
// `MergedTranscriptMarker` is the SLIM shape /api/state embeds per session —
// just the fields a listing reads without rendering (counts, speakers, the
// re-fetch stamp). The full `MergedTranscript` (segments[]/plain_text/…) is
// the /api/transcribe-session response AND the lazy sessionTranscript.fetch
// result that the merged-transcript renderer consumes.
// ---------------------------------------------------------------------------

export interface MergedTranscriptMarker {
  transcribed_at: string | null; // ISO 8601 — null only on malformed on-disk JSON
  segment_count: number;
  suppressed_count: number;
  speakers: string[]; // main.js derives its speaker-alias key set from this
}

// `SummaryMarker` is the SLIM shape /api/state embeds per session — the
// re-fetch stamp plus which source/engine produced the summary. The full
// `PersistedSummary` body is fetched lazily via sessionSummary.fetch.
export interface SummaryMarker {
  summarized_at: string | null; // ISO 8601 — null only on malformed on-disk JSON
  source: string;
  model: string; // empty for the command source
  // ISO 8601 stamp of the merged transcript this summary was built from (#94).
  // Compared against the live `session_transcript` marker so the view can flag a
  // summary that predates a later re-transcribe. Null for summaries written
  // before this field existed.
  transcribed_at: string | null;
}

// GET /api/sessions/{session}/summary response — the persisted summary body.
export interface PersistedSummary {
  summary: string;
  source: string;
  prompt: string;
  model: string;
  command: string;
  took_ms: number;
  created_at: string;
  summarized_at: string;
  // Stamp of the merged transcript this summary was built from (#94); null for
  // summaries persisted before this field was added.
  transcribed_at: string | null;
}

export interface MergedTranscript {
  session: string;
  model: string;
  transcriber: string;
  backend: string;
  device: string;
  source: "original" | "stripped";
  from_iso: string | null;
  to_iso: string | null;
  transcribed_at: string; // ISO 8601
  transcribe_ms: number;
  wav_count: number;
  skipped_bad_count: number;
  skipped_silent_count: number;
  skipped_no_cache: string[];
  speakers: string[];
  speaking_seconds: Record<string, number>;
  segments: Segment[];
  suppressed: SuppressedSegment[];
  suppressed_count: number;
  plain_text: string;
  low_confidence_count: number;
  source_language: string;
}

export interface Segment {
  abs_start: string; // ISO 8601
  abs_end: string;   // ISO 8601
  speaker: string;
  text: string;
  source_wav: string;
  low_confidence: boolean;
  avg_logprob?: number;
}

export interface SuppressedSegment {
  abs_start: string; // ISO 8601
  speaker: string;
  text: string;
  matched_rule: string;
  source_wav: string;
}

// Operator-tunable knobs for /api/sessions/{session}/strip-silence.
// Mirrors STRIP_OPT_DEFAULTS in main.js — keep in sync.
export interface StripOpts {
  min_silence_ms: number;
  pad_ms: number;
  speech_floor_db: number;
}

// /api/sessions/{session}/strip-silence response
export interface StripSilenceResult {
  ok: boolean;
  session: string;
  files_processed: number;
  files_written: number;
  in_seconds: number;
  speech_seconds: number;
  detector: string | string[];
  stripped_at: string;
  took_ms: number;
  files: { name?: string; segments?: number; written?: boolean; region_spans?: CutSpan[]; [k: string]: unknown }[];
}

// ---------------------------------------------------------------------------
// Model catalog — /api/models response
// ---------------------------------------------------------------------------

export interface ModelCatalog {
  context: "batch" | "live";
  available_backends: string[];
  models: ModelEntry[];
}

export interface ModelEntry {
  model_id: string;
  family: string;
  display_name: string;
  description: string;
  languages: string[];
  contexts: string[];
  backends: string[];
  inputs: ModelInput[];
  available: boolean;
}

// The one UI form-field kind a model can declare it accepts.
// Source: tapscribe/transcribers/base.py TextInput.to_mapping().
export type ModelInput = TextInput;

export interface TextInput {
  type: "text";
  name: string;
  label: string;
  kind: "text" | "textarea";
  placeholder: string;
  description: string;
}

// ---------------------------------------------------------------------------
// Frontend-only shapes
// ---------------------------------------------------------------------------

// effectiveMeta() return: local optimistic override merged with server meta.
export interface EffectiveMeta {
  label: string;
  aliases: Record<string, string>;
  prompt: string;
  hotwords: string;
  languages: string[];
}

// GET /api/search — one hit per matching session.
export interface SearchHit {
  session: string;
  label: string;
  snippet: string;
  count: number;
}

// GET /api/languages — the candidate-language catalog (ADR-0010): the full
// selectable allowlist (code + display name), the operator's current global
// default, and the specialist table (language → the extra model the cover adds,
// ADR-0011) the Transcript readout uses to name what a transcribe will run.
// Fetched once at boot (like ModelCatalog).
export interface LanguageCatalog {
  languages: { code: string; name: string }[];
  default: string[];
  specialists: SpecialistMap;
}

// --- Component ctx objects (passed from main.js into each render() call) ---

export interface RibbonCtx {
  statusEl: HTMLElement;
  pillEl: HTMLElement;
}

export interface LiveFeedCtx {
  countEl: HTMLElement;
  shell: HTMLElement;
  autoscrollEl: HTMLElement;
  // The focused session id; the panel renders only this session's lines. ""
  // (no session focused) renders nothing. Drives entriesForSession + the sig.
  sessionId: string;
  // Whether the focused session is the live (current) one — picks the empty
  // state ("awaiting" vs "not recording") and gates the global Clear button.
  isCurrent: boolean;
}

export interface ActiveTapsCtx {
  countEl: HTMLElement;
  badgeEl: HTMLElement;
  bodyEl: HTMLElement;
}

export interface LiveChannelCtx {
  stateEl: HTMLElement;
  /** Acceleration note (historical name) — filled from available_backends. */
  mlxEl: HTMLElement;
  bodyEl: HTMLElement;
  // `start` is handed the bodyEl it was invoked from (see live-channel.js's
  // formValues(host)) so the caller reads the right instance's form when two
  // views (Capture + Taps) each hold their own live-channel body.
  onAction: { start: (host: HTMLElement) => void; stop: () => void };
  liveCatalog: ModelCatalog;
}

export interface ConfigCardCtx {
  gridEl: HTMLElement;
  headerNoteEl: HTMLElement;
  // Stages Settings only: gate the batch prompt/hotwords editors on a SPECIFIC
  // model's declared inputs (the "Default engine" selection) instead of the
  // registry-wide inputs_support. Classic dashboard omits it → unchanged.
  supportOverride?: { batch_prompt: boolean; batch_hotwords: boolean } | null;
  // Show the "N sessions override this" footnote (default true; Stages passes
  // false — its global defaults don't surface per-session override counts).
  showOverrideCounts?: boolean;
}

// (SessionSidebarCtx / SessionDetailCtx were removed with the classic
// dashboard — the Stages views type their contexts inline via JSDoc.)

// Dev/test-only renderRegion sig-drift audit (templates.js `_auditSigCoversOutput`).
// Unset in production. When `__TAPSCRIBE_SIG_AUDIT` is true, a skipped renderRegion
// re-builds into a probe and records any region whose output drifts from its sig.
declare global {
  // eslint-disable-next-line no-var
  var __TAPSCRIBE_SIG_AUDIT: boolean | undefined;
  // eslint-disable-next-line no-var
  var __TAPSCRIBE_SIG_DRIFT: Array<{ sig: string; expected: string; actual: string }> | undefined;
  // Dev/test-only census of drift probes that actually RAN, by kind. Zero drift
  // is only evidence when the probe fired: an audit over empty views passes
  // vacuously, which is how the keyed-list row probe was nominally covered but
  // never exercised. Per KIND, not one total — a total was satisfied by whichever
  // probe happened to fire, leaving the other kinds' "no drift" meaningless.
  // eslint-disable-next-line no-var
  var __TAPSCRIBE_SIG_PROBES: { region: number; list: number; row: number } | undefined;
  // Dev/test-only: how many times renderAll (next/main.js) has actually run —
  // an e2e test's evidence that idle 304 ticks stop re-running it (issue #245).
  // eslint-disable-next-line no-var
  var __TAPSCRIBE_RENDER_ALL_COUNT: number | undefined;
}
