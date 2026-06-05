// =============================================================================
// TapScribe prototype mock data — the ONE canonical scenario.
//
// All three "show and throw" prototypes (studio / console / clarity) import
// this module so they render the *same* meeting and are directly comparable.
// NOTHING here touches the real backend; these are hand-built fixtures that
// also stand in for features the backend does not implement yet (waveform cut
// preview, diarization, cross-session per-mic profiles, dual-language).
//
// Grounded in the real data model so the mock is faithful:
//   - /api/state shape (active taps, live_feed, live_info, sessions)
//   - session-meta.json aliases (identity -> display name)
//   - strip-silence response (regions per WAV, in_seconds / speech_seconds)
//   - transcribers/catalog.py families + Canary source_lang/target_lang
//   - gate knobs: gate_speech_threshold / gate_hangover_ms / gate_pre_roll_ms
// =============================================================================

// --- Languages -------------------------------------------------------------
// flag is an emoji so prototypes need no image assets.
export const LANGS = {
  nb: { code: "nb", name: "Norwegian", flag: "🇳🇴" },
  da: { code: "da", name: "Danish", flag: "🇩🇰" },
  en: { code: "en", name: "English", flag: "🇬🇧" },
  sv: { code: "sv", name: "Swedish", flag: "🇸🇪" },
  de: { code: "de", name: "German", flag: "🇩🇪" },
  fr: { code: "fr", name: "French", flag: "🇫🇷" },
  auto: { code: "auto", name: "Auto-detect", flag: "✨" },
};

// --- People / speakers with cross-session per-mic profiles -----------------
// `mic` keys the saved profile: the same person on the same mic reuses these
// gate/floor/language settings across every session. This is the "store
// configurations per speaker over multiple sessions based on the microphone"
// the operator asked for.
export const SPEAKERS = [
  {
    id: "atle",
    name: "Atle Håvsø",
    initials: "AH",
    spk: 0, // palette slot
    mic: { id: "shure-mv7-usb", label: "Shure MV7" },
    primaryLang: "nb",
    secondaryLang: "en",
    gateThreshold: 0.55,
    noiseFloorDb: -45,
    sessionsSeen: 14,
    note: "Host. Norwegian, switches to English for guests.",
    isRoom: false,
  },
  {
    id: "mette",
    name: "Mette Sørensen",
    initials: "MS",
    spk: 1,
    mic: { id: "airpods-pro-2", label: "AirPods Pro 2" },
    primaryLang: "da",
    secondaryLang: "en",
    gateThreshold: 0.5,
    noiseFloorDb: -42,
    sessionsSeen: 9,
    note: "Danish. AirPods = noisier floor, higher gate.",
    isRoom: false,
  },
  {
    id: "room-oslo",
    name: "Oslo Conference Room",
    initials: "OR",
    spk: 2,
    mic: { id: "jabra-speak-710", label: "Jabra Speak 710" },
    primaryLang: "nb",
    secondaryLang: "en",
    gateThreshold: 0.45,
    noiseFloorDb: -40,
    sessionsSeen: 6,
    note: "Shared room mic — multiple people. Needs diarization.",
    isRoom: true,
    // diarization splits this single tap into who-spoke-when
    diarizedInto: [
      { label: "Speaker A", spk: 3, lang: "nb", talkPct: 58 },
      { label: "Speaker B", spk: 4, lang: "en", talkPct: 42 },
    ],
  },
  {
    id: "james",
    name: "James Park",
    initials: "JP",
    spk: 4,
    mic: { id: "macbook-builtin", label: "MacBook built-in" },
    primaryLang: "en",
    secondaryLang: null,
    gateThreshold: 0.6,
    noiseFloorDb: -38,
    sessionsSeen: 2,
    note: "Guest. English only, laptop mic — clips easily.",
    isRoom: false,
  },
];

export const speakerById = (id) => SPEAKERS.find((s) => s.id === id);

// --- App / recorder state (mirrors /api/state top-level) -------------------
export const APP = {
  name: "TapScribe",
  version: "0.9.0-proto",
  recordingEnabled: true,
  backends: [
    { kind: "auto", label: "auto", available: true },
    { kind: "mlx", label: "mlx", available: true },
    { kind: "cuda", label: "cuda", available: false }, // grayed out
    { kind: "cpu", label: "cpu", available: true },
  ],
  backend: "mlx", // currently resolved
};

// --- Model catalog (mirrors transcribers/catalog.py families) --------------
// `inputs` mirrors ModelInput: text/textarea/select. Canary exposes
// source_lang/target_lang selects (translation-capable).
export const MODELS = [
  {
    family: "whisper",
    models: [
      { id: "small.en", display: "small.en", desc: "fast English-only", langs: ["en"] },
      { id: "large-v3", display: "large-v3", desc: "multilingual, slow", langs: ["auto"] },
    ],
  },
  {
    family: "nb-whisper",
    models: [
      { id: "nb-whisper-medium", display: "nb-whisper-medium", desc: "Norwegian", langs: ["nb"] },
    ],
  },
  {
    family: "voxtral",
    models: [
      { id: "voxtral-mini", display: "voxtral-mini", desc: "8 langs", langs: ["en", "da", "de", "fr"] },
    ],
  },
  {
    family: "parakeet",
    models: [
      { id: "parakeet-tdt-0.6b-v3", display: "parakeet-tdt-0.6b-v3", desc: "25 EU langs", langs: ["en", "nb", "da", "de"] },
    ],
  },
  {
    family: "canary",
    models: [
      {
        id: "canary-1b-v2",
        display: "canary-1b-v2",
        desc: "25 EU langs + translation",
        langs: ["en", "nb", "da", "de", "sv", "fr"],
        inputs: [
          { kind: "select", name: "source_lang", label: "Source language", default: "nb" },
          { kind: "select", name: "target_lang", label: "Target language", default: "en" },
          { kind: "text", name: "hotwords", label: "Hotwords", placeholder: "Acme, Vortiago…" },
        ],
      },
    ],
  },
];

export const selectedModel = { backend: "mlx", family: "canary", model: "canary-1b-v2", sourceLang: "nb", targetLang: "en" };

// --- Live taps (active streams, mirrors /api/state `active`) ----------------
// Atle + the Oslo room are live right now; level is a 0..1 meter, lag in s.
export const LIVE_TAPS = [
  {
    identity: "atle",
    name: "Atle Håvsø",
    spk: 0,
    level: 0.62,
    lagS: 0.8,
    record: true,
    live: true,
    gateOpen: true,
    lang: "nb",
    buffer: "…så hvis vi ser på tallene fra forrige", // in-flight hypothesis
    levels: [0.1, 0.2, 0.5, 0.7, 0.62, 0.4, 0.55, 0.7, 0.66, 0.5, 0.6, 0.62], // sparkline
  },
  {
    identity: "room-oslo",
    name: "Oslo Conference Room",
    spk: 2,
    level: 0.38,
    lagS: 1.6,
    record: true,
    live: true,
    gateOpen: true,
    lang: "nb",
    diarized: "Speaker B",
    buffer: "I think the numbers look", // English speaker currently
    levels: [0.2, 0.3, 0.25, 0.4, 0.38, 0.3, 0.45, 0.5, 0.42, 0.3, 0.36, 0.38],
  },
  {
    identity: "mette",
    name: "Mette Sørensen",
    spk: 1,
    level: 0.0,
    lagS: 0.0,
    record: true,
    live: true,
    gateOpen: false,
    lang: "da",
    buffer: "",
    levels: [0, 0, 0, 0.05, 0, 0, 0, 0.02, 0, 0, 0, 0],
  },
  {
    identity: "james",
    name: "James Park",
    spk: 4,
    level: 0.0,
    lagS: 0.0,
    record: false, // operator paused recording for the guest
    live: true,
    gateOpen: false,
    lang: "en",
    buffer: "",
    levels: [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
  },
];

// --- Live captions feed (mirrors /api/state `live_feed`) -------------------
// Settled lines, attributed to speaker + the language each was transcribed as.
export const LIVE_CAPTIONS = [
  { t: 142.0, speaker: "Atle Håvsø", spk: 0, lang: "nb", text: "God morgen alle sammen, takk for at dere kunne møtes." },
  { t: 151.3, speaker: "Mette Sørensen", spk: 1, lang: "da", text: "Tak, godt at være med. Kan I høre mig fint?" },
  { t: 158.7, speaker: "Atle Håvsø", spk: 0, lang: "nb", text: "Ja, helt klart. La oss starte med tallene." },
  { t: 167.2, speaker: "Oslo Room · Speaker B", spk: 4, lang: "en", text: "Quick question before we dive in — is the dashboard shared?" },
  { t: 174.9, speaker: "Oslo Room · Speaker A", spk: 3, lang: "nb", text: "Ja, jeg deler skjermen nå." },
  { t: 182.1, speaker: "Atle Håvsø", spk: 0, lang: "nb", text: "…så hvis vi ser på tallene fra forrige kvartal", inflight: true },
];

// --- Sessions list (mirrors /api/state `sessions`) -------------------------
export const SESSIONS = [
  {
    id: "2026-05-28T09-00-00Z",
    label: "Nordic Sync",
    folder: "recordings/2026-05-28T09-00-00Z",
    startedAt: "2026-05-28T09:00:00Z",
    durationS: 2880, // 48 min
    wavCount: 37,
    speakers: ["atle", "mette", "room-oslo", "james"],
    current: true,
    hasTranscript: true,
    langs: ["nb", "da", "en"],
  },
  {
    id: "2026-05-21T13-30-00Z",
    label: "1:1 Atle / Mette",
    folder: "recordings/2026-05-21T13-30-00Z",
    startedAt: "2026-05-21T13:30:00Z",
    durationS: 1620,
    wavCount: 22,
    speakers: ["atle", "mette"],
    current: false,
    hasTranscript: true,
    langs: ["nb", "da"],
  },
  {
    id: "2026-05-19T10-00-00Z",
    label: "Design review",
    folder: "recordings/2026-05-19T10-00-00Z",
    startedAt: "2026-05-19T10:00:00Z",
    durationS: 3300,
    wavCount: 51,
    speakers: ["atle", "james", "room-oslo"],
    current: false,
    hasTranscript: false,
    langs: ["en", "nb"],
  },
  {
    id: "2026-05-12T08-45-00Z",
    label: "",
    folder: "recordings/2026-05-12T08-45-00Z",
    startedAt: "2026-05-12T08:45:00Z",
    durationS: 540,
    wavCount: 8,
    speakers: ["atle"],
    current: false,
    hasTranscript: true,
    langs: ["nb"],
  },
];

// --- Strip-silence knobs (operator-tunable; real defaults) -----------------
export const STRIP_DEFAULTS = { minSilenceMs: 500, padMs: 200, speechFloorDb: -45 };

// --- A representative WAV for the waveform + cut preview --------------------
// Atle's ~48 s recording with 4 speech bursts separated by silence. We
// generate the peak envelope deterministically (seeded) so every prototype
// draws the identical waveform without shipping a big literal array.
const REP_WAV_DURATION_S = 48;
const PEAKS_PER_S = 16;

function mulberry32(seed) {
  return function () {
    seed |= 0;
    seed = (seed + 0x6d2b79f5) | 0;
    let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

// [startS, endS, gain] speech bursts; everything else is near-silent floor.
const BURSTS = [
  [1.5, 9.0, 0.92],
  [12.5, 21.5, 0.8],
  [24.0, 32.5, 0.95],
  [37.5, 46.0, 0.85],
];

function buildPeaks() {
  const rng = mulberry32(0x7a9c01);
  const n = Math.round(REP_WAV_DURATION_S * PEAKS_PER_S);
  const peaks = new Array(n);
  for (let i = 0; i < n; i++) {
    const t = i / PEAKS_PER_S;
    // Background floor ~ -56..-53 dBFS — must sit BELOW the default -45 dBFS
    // speech floor so silences read as silence and the 4 bursts split cleanly.
    let amp = 0.0012 + rng() * 0.001;
    for (const [s, e, g] of BURSTS) {
      if (t >= s && t <= e) {
        // raised-cosine envelope inside the burst + voiced jitter
        const p = (t - s) / (e - s);
        const env = Math.sin(Math.PI * Math.min(1, Math.max(0, p)));
        const voiced = 0.55 + 0.45 * Math.abs(Math.sin(t * 5.3));
        amp = Math.max(amp, g * (0.25 + 0.75 * env) * voiced * (0.7 + rng() * 0.6));
      }
    }
    peaks[i] = Math.min(1, amp);
  }
  return peaks;
}

export const REP_WAV = {
  name: "2026-05-28T09-04-12Z_atle_atle_a1b2c3d4.wav",
  speakerId: "atle",
  durationS: REP_WAV_DURATION_S,
  peaksPerS: PEAKS_PER_S,
  peaks: buildPeaks(),
};

// --- Live re-cut: compute strip-silence regions from peaks + knobs ----------
// A faithful-enough stand-in for the real silero + low-energy-filter pipeline:
// threshold by dBFS floor, split on silence gaps >= minSilenceMs, pad each
// region by padMs, drop sub-200ms slivers. Identical across all prototypes so
// dragging a knob re-cuts the same way everywhere.
export function computeRegions(peaks, durationS, knobs) {
  const { minSilenceMs, padMs, speechFloorDb } = { ...STRIP_DEFAULTS, ...knobs };
  const dt = durationS / peaks.length; // seconds per peak
  const floorAmp = Math.pow(10, speechFloorDb / 20);
  // 1. boolean speech mask
  const speech = peaks.map((p) => p >= floorAmp);
  // 2. collect raw speech runs
  const runs = [];
  let start = -1;
  for (let i = 0; i < speech.length; i++) {
    if (speech[i] && start < 0) start = i;
    if ((!speech[i] || i === speech.length - 1) && start >= 0) {
      const end = speech[i] ? i + 1 : i;
      runs.push([start, end]);
      start = -1;
    }
  }
  // 3. merge runs whose silence gap < minSilenceMs
  const gapPeaks = minSilenceMs / 1000 / dt;
  const merged = [];
  for (const r of runs) {
    if (merged.length && r[0] - merged[merged.length - 1][1] < gapPeaks) {
      merged[merged.length - 1][1] = r[1];
    } else {
      merged.push([...r]);
    }
  }
  // 4. pad + convert to seconds, drop slivers (< 200 ms)
  const padPeaks = padMs / 1000 / dt;
  const regions = [];
  for (const [a, b] of merged) {
    const s = Math.max(0, (a - padPeaks) * dt);
    const e = Math.min(durationS, (b + padPeaks) * dt);
    if (e - s >= 0.2) regions.push({ startS: +s.toFixed(2), endS: +e.toFixed(2) });
  }
  const speechS = regions.reduce((acc, r) => acc + (r.endS - r.startS), 0);
  return { regions, speechS: +speechS.toFixed(1), totalS: durationS, clips: regions.length };
}

// --- Merged transcript (mirrors session-transcript.json) -------------------
// Mixed nb/da/en, speaker-colored, one low-confidence line, one suppressed
// hallucination, a translation badge (Canary nb->en on one line).
export const TRANSCRIPT = {
  model: "canary-1b-v2",
  backend: "mlx",
  durationS: 2880,
  translated: true, // some lines carry source_lang != target_lang
  speakingTime: [
    { speaker: "Atle Håvsø", spk: 0, pct: 41 },
    { speaker: "Mette Sørensen", spk: 1, pct: 23 },
    { speaker: "Oslo Room · Speaker A", spk: 3, pct: 19 },
    { speaker: "Oslo Room · Speaker B", spk: 4, pct: 17 },
  ],
  lines: [
    { t: 5.2, speaker: "Atle Håvsø", spk: 0, lang: "nb", text: "God morgen alle sammen, takk for at dere kunne møtes." },
    { t: 12.8, speaker: "Mette Sørensen", spk: 1, lang: "da", text: "Tak, godt at være med. Kan I høre mig fint?" },
    { t: 19.4, speaker: "Atle Håvsø", spk: 0, lang: "nb", text: "Ja, helt klart. La oss starte med kvartalstallene." },
    { t: 27.1, speaker: "Oslo Room · Speaker A", spk: 3, lang: "nb", text: "Jeg deler skjermen nå — ser dere grafen?" },
    { t: 34.6, speaker: "Oslo Room · Speaker B", spk: 4, lang: "en", text: "Yes, looks good. The revenue line is up eleven percent.", translatedFrom: null },
    { t: 41.0, speaker: "Mette Sørensen", spk: 1, lang: "da", text: "[mm-hmm]", lowConfidence: true, confidence: 0.31 },
    { t: 47.9, speaker: "Atle Håvsø", spk: 0, lang: "nb", text: "Det stemmer. Oversatt: the growth is mostly from the Nordic segment.", translatedFrom: "nb" },
    { t: 55.2, speaker: "Oslo Room · Speaker B", spk: 4, lang: "en", text: "Thank you for watching, please subscribe.", suppressed: true, matchedRule: "youtube-outro" },
    { t: 61.8, speaker: "Atle Håvsø", spk: 0, lang: "nb", text: "Da går vi videre til neste punkt på agendaen." },
  ],
};

// --- Small shared formatters (optional; reduces per-prototype bugs) ---------
export const helpers = {
  clock(sec) {
    const s = Math.max(0, Math.round(sec));
    const m = Math.floor(s / 60);
    const r = s % 60;
    return `${m}:${String(r).padStart(2, "0")}`;
  },
  clockH(sec) {
    const s = Math.max(0, Math.round(sec));
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const r = s % 60;
    const mm = String(m).padStart(2, "0");
    const ss = String(r).padStart(2, "0");
    return h ? `${h}:${mm}:${ss}` : `${m}:${ss}`;
  },
  pct(n) {
    return `${Math.round(n)}%`;
  },
  lang(code) {
    return LANGS[code] || LANGS.auto;
  },
};

// =============================================================================
// ADDITIVE fixtures (appended for the Stages rebuild). These are NET-NEW exports
// only — nothing above is changed or removed, so the other prototypes that
// import the original names keep rendering the identical scenario. Everything
// below is either (a) a richer view of data already implied by SPEAKERS/APP, or
// (b) a faithful stand-in for a real TapScribe API/setting that the original
// fixtures didn't spell out (gate LiveConfig knobs, hallucination-rule files,
// prompt/hotwords config files, the per-WAV transcript cache).
// =============================================================================

// --- Speech-gate LiveConfig defaults (real /api/state live_config knobs) -----
// These govern how every tap is gated. `gate_kind="tapscribe"` is the per-tap
// silero gate; "backend" defers to the model's own VAD and is unavailable here.
export const GATE_DEFAULTS = {
  gate_kind: "tapscribe", // "tapscribe" | "backend"
  gate_speech_threshold: 0.5, // 0..1
  gate_hangover_ms: 400, // 0..10000
  gate_pre_roll_ms: 300, // 0..5000
  gate_min_speech_ms: 0, // 0..5000
  confidence_validation: true,
};
export const GATE_KINDS = [
  { kind: "tapscribe", label: "tapscribe", available: true },
  { kind: "backend", label: "backend", available: false }, // grey: unsupported here
];

// --- Persons registry (canonical, derived-and-enriched from SPEAKERS) --------
// The real backend piece here is the per-session alias (identity -> display
// name). Everything else is NET-NEW UI: a person can own MULTIPLE microphone
// profiles (each carrying its own gate threshold + noise floor), and multiple
// taps/identities can map to one Person. `mics` is a superset of the single
// `SPEAKERS[].mic`, so the canonical scenario still lines up.
export const PERSONS = [
  {
    id: "atle",
    name: "Atle Håvsø",
    initials: "AH",
    spk: 0,
    primaryLang: "nb",
    secondaryLang: "en",
    sessionsSeen: 14,
    note: "Host. Norwegian, switches to English for guests.",
    isRoom: false,
    mics: [
      { id: "shure-mv7-usb", label: "Shure MV7", gateThreshold: 0.55, noiseFloorDb: -45, primary: true },
      { id: "airpods-pro-2-atle", label: "AirPods Pro 2", gateThreshold: 0.5, noiseFloorDb: -41, primary: false },
    ],
    identities: ["atle"], // taps/identities mapped to this Person
  },
  {
    id: "mette",
    name: "Mette Sørensen",
    initials: "MS",
    spk: 1,
    primaryLang: "da",
    secondaryLang: "en",
    sessionsSeen: 9,
    note: "Danish. AirPods = noisier floor, higher gate.",
    isRoom: false,
    mics: [
      { id: "airpods-pro-2", label: "AirPods Pro 2", gateThreshold: 0.5, noiseFloorDb: -42, primary: true },
    ],
    identities: ["mette"],
  },
  {
    id: "room-oslo",
    name: "Oslo Conference Room",
    initials: "OR",
    spk: 2,
    primaryLang: "nb",
    secondaryLang: "en",
    sessionsSeen: 6,
    note: "Shared room mic — multiple people. Diarized into A/B.",
    isRoom: true,
    mics: [
      { id: "jabra-speak-710", label: "Jabra Speak 710", gateThreshold: 0.45, noiseFloorDb: -40, primary: true },
    ],
    identities: ["room-oslo"],
    diarizedInto: [
      { label: "Speaker A", spk: 3, lang: "nb", talkPct: 58 },
      { label: "Speaker B", spk: 4, lang: "en", talkPct: 42 },
    ],
  },
  {
    id: "james",
    name: "James Park",
    initials: "JP",
    spk: 4,
    primaryLang: "en",
    secondaryLang: null,
    sessionsSeen: 2,
    note: "Guest. English only, laptop mic — clips easily.",
    isRoom: false,
    mics: [
      { id: "macbook-builtin", label: "MacBook built-in", gateThreshold: 0.6, noiseFloorDb: -38, primary: true },
    ],
    identities: ["james"],
  },
];
export const personById = (id) => PERSONS.find((p) => p.id === id);

// --- Tap -> Person mapping (which live identity resolves to which Person) -----
// Mirrors how the recorder ties an incoming /tap identity to a saved alias.
export const TAP_PERSON_MAP = {
  atle: "atle",
  "room-oslo": "room-oslo",
  mette: "mette",
  james: "james",
};

// --- Hallucination rules (real session-hallucinations.json format) -----------
// Three rule kinds: plain substring, `exact:` prefix, `re:` regex. `kind` is
// derived from the stored string's prefix; `display` is what the operator typed.
export const HALLUCINATION_RULES = [
  { display: "thank you for watching", kind: "substring", note: "YouTube outro leak" },
  { display: "please subscribe", kind: "substring", note: "outro leak" },
  { display: "exact:[mm-hmm]", kind: "exact", note: "backchannel filler" },
  { display: "re:^\\s*(uh+|um+)\\s*$", kind: "regex", note: "lone hesitation tokens" },
  { display: "re:tekst af .*", kind: "regex", note: "Danish subtitle credit" },
];

// --- Prompt / live-prompt / hotwords config (real /api/config/{key} files) ---
// Separate global and live-only prompts plus a shared hotword list.
export const PROMPTS = {
  prompt: "Nordic Sync — quarterly product review. Speakers discuss revenue, the Nordic segment, KPIs and the Vortiago launch.",
  livePrompt: "Quarterly review. Proper nouns: Vortiago, Nordic, KPI. Keep punctuation light for live captions.",
  hotwords: "Vortiago\nNordic\nKPI\nHåvsø\nSørensen",
};

// --- Per-WAV transcript cache (a WAV can hold multiple backend/model runs) ----
// Mirrors the on-disk *.transcript.json cache keyed by (backend, model). The
// operator picks which cached run is the WAV's `primary`. `source` is whether
// the run was over the original WAV or its stripped region clips.
export const WAV_TRANSCRIPTS = [
  { backend: "mlx", model: "canary-1b-v2", source: "stripped", words: 142, avgLogprob: -0.21, primary: true },
  { backend: "mlx", model: "nb-whisper-medium", source: "stripped", words: 138, avgLogprob: -0.27, primary: false },
  { backend: "cpu", model: "small.en", source: "original", words: 121, avgLogprob: -0.44, primary: false },
];

// --- A per-session transcription job (one job per session; current/total) ----
export const TRANSCRIBE_JOB = { running: true, current: 23, total: 37, wav: "…090921_atle.wav" };

// =============================================================================
// CORRECTED Tap / Input / Person model (Stages rebuild — additive only).
//
// The earlier fixtures conflated three distinct things; these NEW exports keep
// them separate and match the real backend's per-identity tap settings:
//
//   Tap     — an incoming audio STREAM from a Bridge, keyed by `identity`. It
//             OWNS its audio settings (gate threshold, noise floor, the
//             speech-gate LiveConfig, and rec/live). Those persist PER IDENTITY
//             across sessions. Each tap has exactly one INPUT kind and carries
//             ONE Person, or SEVERAL when diarization splits it.
//   Input   — the KIND of audio a tap brings in: microphone | line-in |
//             stereo-mix (system / file / video audio). Replaces "device"/"mic"
//             as the thing a tap is. NOT a Person attribute.
//   Person  — a canonical HUMAN: name + primary/secondary language (+ a
//             "transcribe as" quick-switch) + the taps/voices mapped to them.
//             A Person has NO gate/floor/input — those live on the Tap.
//
// Speaker = Person: a tap's diarized voices ("Speaker A/B") are not-yet-
// identified People; each maps to a canonical Person or is left Unidentified.
// A room or a stereo-mix is a TAP, never a Person.
// =============================================================================

// --- Input kinds (what a tap brings in) --------------------------------------
// The label/icon source of truth so every view names inputs identically.
export const INPUT_KINDS = {
  microphone: { kind: "microphone", label: "microphone", icon: "🎙️", note: "a personal mic — one speaker" },
  "line-in": { kind: "line-in", label: "line-in", icon: "🔌", note: "a hardware line input" },
  "stereo-mix": { kind: "stereo-mix", label: "stereo-mix", icon: "🎚️", note: "system / file / video audio — often several speakers" },
};
export const inputKind = (kind) => INPUT_KINDS[kind] || INPUT_KINDS.microphone;

// --- Stage taps (incoming streams; each owns its per-identity audio settings) -
// A superset of LIVE_TAPS that carries the corrected model: an `input` kind, the
// per-identity audio settings (gate threshold + noise floor + the speech-gate
// LiveConfig + rec/live) that the real backend remembers per identity, and the
// Person(s) the tap carries. The two personal-mic taps each carry one Person;
// the former "Oslo Conference Room" is now a stereo-mix/room TAP carrying two
// People (one mapped, one Unidentified); a played video/file is a second
// stereo-mix tap to show that Input kind. `voices` lists diarized speakers for a
// multi-person tap (each maps to a Person via TAP_VOICE_PERSON_MAP).
export const STAGE_TAPS = [
  {
    identity: "atle",
    name: "Atle Håvsø",
    spk: 0,
    input: "microphone",
    inputLabel: "Shure MV7 (USB)",
    level: 0.62,
    lagS: 0.8,
    record: true,
    live: true,
    gateOpen: true,
    lang: "nb",
    buffer: "…så hvis vi ser på tallene fra forrige",
    levels: [0.1, 0.2, 0.5, 0.7, 0.62, 0.4, 0.55, 0.7, 0.66, 0.5, 0.6, 0.62],
    // per-identity audio settings the tap OWNS (remembered across sessions)
    settings: {
      gateThreshold: 0.55,
      noiseFloorDb: -45,
      gate_kind: "tapscribe",
      gate_speech_threshold: 0.55,
      gate_hangover_ms: 400,
      gate_pre_roll_ms: 300,
      gate_min_speech_ms: 0,
      confidence_validation: true,
    },
    voices: null, // single-person tap
  },
  {
    identity: "room-oslo",
    name: "Oslo room (Jabra Speak 710)",
    spk: 2,
    input: "stereo-mix",
    inputLabel: "Jabra Speak 710 · room mix",
    level: 0.38,
    lagS: 1.6,
    record: true,
    live: true,
    gateOpen: true,
    lang: "nb",
    buffer: "I think the numbers look",
    levels: [0.2, 0.3, 0.25, 0.4, 0.38, 0.3, 0.45, 0.5, 0.42, 0.3, 0.36, 0.38],
    settings: {
      gateThreshold: 0.45,
      noiseFloorDb: -40,
      gate_kind: "tapscribe",
      gate_speech_threshold: 0.45,
      gate_hangover_ms: 600,
      gate_pre_roll_ms: 400,
      gate_min_speech_ms: 0,
      confidence_validation: true,
    },
    // diarization splits this one stream into who-spoke-when. Each voice maps
    // to a Person (or is left Unidentified) via TAP_VOICE_PERSON_MAP.
    voices: [
      { voiceId: "room-oslo#A", label: "Speaker A", spk: 3, lang: "nb", talkPct: 58 },
      { voiceId: "room-oslo#B", label: "Speaker B", spk: 4, lang: "en", talkPct: 42 },
    ],
  },
  {
    identity: "mette",
    name: "Mette Sørensen",
    spk: 1,
    input: "microphone",
    inputLabel: "AirPods Pro 2",
    level: 0.0,
    lagS: 0.0,
    record: true,
    live: true,
    gateOpen: false,
    lang: "da",
    buffer: "",
    levels: [0, 0, 0, 0.05, 0, 0, 0, 0.02, 0, 0, 0, 0],
    settings: {
      gateThreshold: 0.5,
      noiseFloorDb: -42,
      gate_kind: "tapscribe",
      gate_speech_threshold: 0.5,
      gate_hangover_ms: 400,
      gate_pre_roll_ms: 300,
      gate_min_speech_ms: 0,
      confidence_validation: true,
    },
    voices: null,
  },
  {
    identity: "james",
    name: "James Park",
    spk: 4,
    input: "line-in",
    inputLabel: "Focusrite 2i2 · input 1",
    level: 0.0,
    lagS: 0.0,
    record: false, // operator paused recording for the guest
    live: true,
    gateOpen: false,
    lang: "en",
    buffer: "",
    levels: [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
    settings: {
      gateThreshold: 0.6,
      noiseFloorDb: -38,
      gate_kind: "tapscribe",
      gate_speech_threshold: 0.6,
      gate_hangover_ms: 350,
      gate_pre_roll_ms: 250,
      gate_min_speech_ms: 0,
      confidence_validation: false,
    },
    voices: null,
  },
  {
    identity: "demo-clip",
    name: "Vortiago launch clip",
    spk: 1,
    input: "stereo-mix",
    inputLabel: "system audio · played video/file",
    level: 0.21,
    lagS: 0.4,
    record: true,
    live: true,
    gateOpen: true,
    lang: "en",
    buffer: "…and that's how the rollout will work",
    levels: [0.1, 0.18, 0.21, 0.16, 0.22, 0.2, 0.24, 0.19, 0.21, 0.17, 0.2, 0.21],
    settings: {
      gateThreshold: 0.4,
      noiseFloorDb: -50,
      gate_kind: "tapscribe",
      gate_speech_threshold: 0.4,
      gate_hangover_ms: 500,
      gate_pre_roll_ms: 350,
      gate_min_speech_ms: 0,
      confidence_validation: true,
    },
    // a played video can carry several voices too — narrator mapped, a second
    // voice still Unidentified.
    voices: [
      { voiceId: "demo-clip#A", label: "Speaker A", spk: 1, lang: "en", talkPct: 71 },
      { voiceId: "demo-clip#B", label: "Speaker B", spk: 3, lang: "en", talkPct: 29 },
    ],
  },
];
export const stageTapById = (identity) => STAGE_TAPS.find((t) => t.identity === identity);

// --- People registry (HUMANS ONLY) -------------------------------------------
// A Person holds ONLY: name + primary/secondary language (+ the "transcribe as"
// quick switch, defaulting to primaryLang) + a note. NO gate / noise-floor /
// input profile — those moved to the Tap. The former "Oslo Conference Room" is
// GONE from here (it's a Tap); the two humans heard in that room are real
// People below: Henrik (named) and one voice that stays Unidentified, which is
// represented in TAP_VOICE_PERSON_MAP as a null mapping rather than a Person.
export const STAGE_PEOPLE = [
  {
    id: "atle",
    name: "Atle Håvsø",
    initials: "AH",
    spk: 0,
    primaryLang: "nb",
    secondaryLang: "en",
    sessionsSeen: 14,
    note: "Host. Norwegian, switches to English for guests.",
  },
  {
    id: "mette",
    name: "Mette Sørensen",
    initials: "MS",
    spk: 1,
    primaryLang: "da",
    secondaryLang: "en",
    sessionsSeen: 9,
    note: "Danish, comfortable in English.",
  },
  {
    id: "henrik",
    name: "Henrik Lie",
    initials: "HL",
    spk: 3,
    primaryLang: "nb",
    secondaryLang: "en",
    sessionsSeen: 4,
    note: "Often joins from the Oslo room mic — diarized as a room voice.",
  },
  {
    id: "james",
    name: "James Park",
    initials: "JP",
    spk: 4,
    primaryLang: "en",
    secondaryLang: null,
    sessionsSeen: 2,
    note: "Guest. English only.",
  },
];
export const stagePersonById = (id) => STAGE_PEOPLE.find((p) => p.id === id);

// --- Tap / voice -> Person mapping (Speaker = Person) ------------------------
// Keys are either a tap `identity` (single-person tap) or a diarized
// `voiceId` (multi-person tap). A `null` value means the voice is NOT YET
// identified ("Unidentified — map to a Person"). This shows BOTH states: at
// least one room voice maps to a named Person (Henrik), at least one stays
// Unidentified.
export const TAP_VOICE_PERSON_MAP = {
  // single-person taps
  atle: "atle",
  mette: "mette",
  james: "james",
  // Oslo room (stereo-mix tap): Speaker A is Henrik, Speaker B is Unidentified
  "room-oslo#A": "henrik",
  "room-oslo#B": null,
  // played video/file (stereo-mix tap): narrator is Mette (she presents it),
  // the second voice in the clip is Unidentified
  "demo-clip#A": "mette",
  "demo-clip#B": null,
};
// Resolve a tap/voice key to its Person (or null if Unidentified).
export const personForVoice = (key) => {
  const id = TAP_VOICE_PERSON_MAP[key];
  return id ? stagePersonById(id) : null;
};

// Convenience: everything under one namespace too.
export const MOCK = {
  APP,
  LANGS,
  SPEAKERS,
  MODELS,
  selectedModel,
  LIVE_TAPS,
  LIVE_CAPTIONS,
  SESSIONS,
  STRIP_DEFAULTS,
  REP_WAV,
  TRANSCRIPT,
  computeRegions,
  helpers,
  speakerById,
  // additive (Stages rebuild) — new keys only, originals above untouched
  GATE_DEFAULTS,
  GATE_KINDS,
  PERSONS,
  personById,
  TAP_PERSON_MAP,
  HALLUCINATION_RULES,
  PROMPTS,
  WAV_TRANSCRIPTS,
  TRANSCRIBE_JOB,
  // additive (Stages terminology fix) — corrected Tap / Input / Person model
  INPUT_KINDS,
  inputKind,
  STAGE_TAPS,
  stageTapById,
  STAGE_PEOPLE,
  stagePersonById,
  TAP_VOICE_PERSON_MAP,
  personForVoice,
};
export default MOCK;
