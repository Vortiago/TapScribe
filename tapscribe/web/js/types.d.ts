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
  mlx_available: boolean;
  backend: string;
  available_backends: string[];
  recording_enabled: boolean;
  prompt: ConfigFile;
  live_prompt: ConfigFile;
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
  session_transcript: MergedTranscript | null;
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
  transcript: WavTranscript | null;   // primary cached transcript
  transcripts: WavTranscript[];       // all cached model variants
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
  transcript: WavTranscript | null;
  transcripts: WavTranscript[];
  wav_start: string | null;
  wav_end: string | null;
  speaker_name: string;
}

// Cached per-WAV transcript (the primary model's result).
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
// Merged (session-level) transcript — /api/transcribe-session response and
// the session_transcript field embedded in Session above.
// ---------------------------------------------------------------------------

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
  inputs: string[];
  available: boolean;
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
  mlxEl: HTMLElement;
  bodyEl: HTMLElement;
  mlxAvail: boolean;
  onAction: { start: () => void; stop: () => void };
  liveCatalog: ModelCatalog;
}

export interface ConfigCardCtx {
  gridEl: HTMLElement;
  headerNoteEl: HTMLElement;
}

export interface SessionSidebarCtx {
  listEl: HTMLElement;
  selectedId: string | null;
  filter: string;
  metaFor: (s: Session) => EffectiveMeta;
  onSelect: (id: string) => void;
  onDelete: (id: string) => void;
}

export interface SessionDetailCtx {
  // State snapshots
  lastJson: AppState | null;
  batchModel: string;
  batchBackend: string;
  modelCatalog: ModelCatalog;
  sourcePick: Map<string, "original" | "stripped">;
  sessInflight: Map<string, number>;
  sessJustDone: Map<string, number>;
  sessStripInflight: Map<string, number>;
  wavInflight: Map<string, number>;
  wavJustDone: Map<string, number>;
  expandedWav: string | null;
  rangeState: Record<string, Record<string, string>>;
  rxOpen: boolean;
  rxPattern: string;
  rxFlags: string;
  defaults: { prompt: string; hotwords: string };
  // Derived state helpers
  effectiveMeta: (s: Session | null) => EffectiveMeta;
  deriveSpeakerKeys: (s: Session | null) => string[];
  // Sub-component delegate
  renderMerged: (t: MergedTranscript, meta: EffectiveMeta) => void;
  // Mutation callbacks
  onTranscribeSession: (sessId: string) => void;
  onCopyMerged: (sessId: string, btn: HTMLButtonElement) => void;
  onTranscribeWav: (session: string, name: string, sourceOverride?: string | null) => void;
  onToggleWav: (wavKey: string, sess: Session) => void;
  onRangeEdit: (sessKey: string, key: string, value: string) => void;
  onModelChange: (model: string) => void;
  onBackendChange: (backend: string) => void;
  onSourcePick: (sessKey: string, source: "original" | "stripped") => void;
  onStripRun: (sessId: string) => void;
  onStripRemove: (sessId: string) => void;
  // strip-silence operator knobs (added in #58). stripOpts holds the
  // currently-selected values; the inputs in session-detail.js render
  // them and call onStripOptEdit per keystroke (empty string → reset to
  // default for that key, see main.js). onStripOptReset bulk-resets.
  stripOpts: StripOpts;
  onStripOptEdit: <K extends keyof StripOpts>(key: K, value: string) => void;
  onStripOptReset: () => void;
  onNameEdit: (sessKey: string, value: string) => void;
  onAliasEdit: (sessKey: string, speakerKey: string, value: string) => void;
  onMetaOverrideEdit: (sessKey: string, metaKey: string, value: string) => void;
  onAbsorbSession: (target: string, source: string) => void;
  onRxToggle: (sessKey: string) => void;
  onRxPatternInput: (sessKey: string, value: string) => void;
  onRxFlagsInput: (sessKey: string, value: string) => void;
  onRxSeed: (sessKey: string, seed: string) => void;
  onAuditToggle: () => void;
}
