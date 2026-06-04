// Hand-written TypeScript declarations for tapscribe/web/js/.
// Reference from JSDoc (no runtime import):
//   /** @type {import('./types.js').AppState} */
//
// Source ground-truth:
//   tapscribe/app.py           — /api/state + mutation routes
//   tapscribe/recorder.py      — ActiveStream, JobState dataclasses
//   tapscribe/session_merge.py — MergedTranscript, Segment shapes
//   tapscribe/sessions.py      — gather_sessions() per-session shape
//   tapscribe/web/js/main.js   — ctx prop-bags passed to render()

// ---------------------------------------------------------------------------
// /api/state response
// ---------------------------------------------------------------------------

export interface AppState {
  current_session: string;
  active: ActiveStream[];
  sessions: Session[];
  default_override_counts: { prompt: number; hotwords: number };
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
  hotwords: ConfigFile;
  inputs_support: InputsSupport;
  hallucinations: HallucinationsConfig;
}

// Active /tap WebSocket (one per recording utterance). `record` and `live`
// are overwritten at serialisation time with the per-identity TapSetting
// preference (tapscribe/app.py:329-330), not the WS-open snapshot.
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
  rules: string[];
  count: number;
}

// ---------------------------------------------------------------------------
// Session list (gather_sessions output)
// ---------------------------------------------------------------------------

export interface Session {
  session: string;
  wav_count: number;
  files: WavFile[];
  is_current: boolean;
  earliest_iso: string | null;
  latest_iso: string | null;
  // SLIM marker only — /api/state no longer embeds the full merged
  // transcript. The full body (segments[]/plain_text/suppressed[]) is fetched
  // lazily via fetchSessionTranscript(session, transcribed_at), cached
  // client-side. A marker change (new transcribed_at) is the re-fetch signal.
  session_transcript: MergedTranscriptMarker | null;
  progress: JobStateSnapshot | null;
  session_meta: SessionMeta;
  stripped: StrippedStats | null;
}

// Optional — all fields absent when no session-meta.json exists.
export interface SessionMeta {
  label?: string;
  prompt?: string;
  hotwords?: string;
  aliases?: Record<string, string>;
}

export interface StrippedStats {
  count: number;
  speech_seconds: number;
  stripped_at: string | null; // ISO 8601 mtime of stripped/
}

// dataclasses.asdict(JobState) — one in-flight job per session at a time.
export interface JobStateSnapshot {
  session: string;
  kind: "transcribe" | "strip";
  current: number;
  total: number;
  started_at: string; // ISO 8601
  status: string;
  current_file: string | null;
  model: string | null;
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
// compare key). The full body is fetched lazily via fetchWavTranscript.
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
// fetchWavTranscript result, rendered by buildExpandTx.
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

// ---------------------------------------------------------------------------
// Merged (session-level) transcript.
//
// `MergedTranscriptMarker` is the SLIM shape /api/state embeds per session —
// just the fields a listing reads without rendering (counts, speakers, the
// re-fetch stamp). The full `MergedTranscript` (segments[]/plain_text/…) is
// the /api/transcribe-session response AND the lazy fetchSessionTranscript
// result that the merged-transcript renderer consumes.
// ---------------------------------------------------------------------------

export interface MergedTranscriptMarker {
  transcribed_at: string | null; // ISO 8601 — null only on malformed on-disk JSON
  segment_count: number;
  suppressed_count: number;
  speakers: string[]; // main.js derives its speaker-alias key set from this
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
  target_language: string;
  // Defensive: older on-disk session JSON (pre-#48) never wrote this at
  // the MergedTranscript level, but the translate-badge fallback chain
  // (merged-transcript.js render) still reads it for safety. Widen the
  // type rather than narrow the runtime check.
  language?: string;
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
  files: { segments?: number; [k: string]: unknown }[];
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

// Discriminated union of UI form fields each model declares it accepts.
// Source: tapscribe/transcribers/base.py TextInput / SelectInput .to_mapping().
export type ModelInput = TextInput | SelectInput;

export interface TextInput {
  type: "text";
  name: string;
  label: string;
  kind: "text" | "textarea";
  placeholder: string;
  description: string;
}

export interface SelectInput {
  type: "select";
  name: string;
  label: string;
  options: { value: string; label: string }[];
  default: string;
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
  onAction: { start: () => void; stop: () => void };
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
