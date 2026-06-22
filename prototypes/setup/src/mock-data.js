// Mock data for the setup prototypes. Mirrors the SHAPE of the real
// install_picker.py catalog (model families, backends, the [vad]/[summarize]
// runtime extras) but with hand-picked sizes so the screens feel real. None of
// this touches pip — it just drives the mock plan + the fake progress engine.

/** Detected-machine fixtures. The floating bar flips between these so you can
 *  see how each variant adapts (Apple Silicon → MLX, NVIDIA → CUDA, else CPU). */
export const MACHINES = {
  mac: {
    id: "mac",
    label: "MacBook Pro",
    detail: "Apple M3 Pro · 36 GB · macOS 15",
    os: "Darwin",
    arch: "arm64",
    mlx: true,
    cuda: false,
    accel: "Apple Silicon GPU (MLX)",
  },
  nvidia: {
    id: "nvidia",
    label: "Workstation",
    detail: "NVIDIA RTX 4090 · 24 GB VRAM · CUDA 12.4",
    os: "Linux",
    arch: "x86_64",
    mlx: false,
    cuda: true,
    accel: "NVIDIA GPU (CUDA)",
  },
  cpu: {
    id: "cpu",
    label: "Linux server",
    detail: "16-core CPU · no GPU detected",
    os: "Linux",
    arch: "x86_64",
    mlx: false,
    cuda: false,
    accel: "CPU only",
  },
};

/** Transcription model families. `live` = what the live channel can ACTUALLY
 *  launch today, not just what the catalog declares: build_live_cmd (live.py)
 *  only emits `--backend faster-whisper|mlx-whisper` (+ the NB-Whisper
 *  `--model-path` route), so only Whisper-family models stream. Parakeet is
 *  batch-only in the catalog; Voxtral is *declared* `_BATCH_AND_LIVE` but
 *  build_live_cmd has no Voxtral branch (and no test exercises it), so it can't
 *  be streamed through WhisperLiveKit as wired — treated batch-only here.
 *  (Moonshine is catalog-live but inference isn't implemented yet, #122/#123.) */
export const FAMILIES = {
  whisper: {
    key: "whisper",
    label: "Whisper",
    blurb: "General multilingual speech-to-text. Fast, the default.",
    langs: "99 languages",
    live: true,
    sizeMlxMB: 80,
    sizeCpuMB: 150,
    mlxOk: true,
  },
  nbwhisper: {
    key: "nbwhisper",
    label: "NB-Whisper",
    blurb: "Norwegian-tuned Whisper (Nasjonalbiblioteket). Install only if you need Norwegian.",
    langs: "Norwegian (nb/nn)",
    live: true,
    sizeMlxMB: 90,
    sizeCpuMB: 160,
    mlxOk: true,
  },
  parakeet: {
    key: "parakeet",
    label: "Parakeet (NVIDIA)",
    blurb: "Very fast & accurate, 25 European languages (no Norwegian). Batch jobs only.",
    langs: "25 EU languages",
    live: false, // catalog: _BATCH_ONLY — no live channel
    sizeMlxMB: 1500,
    sizeCpuMB: 2500,
    mlxOk: true,
  },
  voxtral: {
    key: "voxtral",
    label: "Voxtral (Mistral)",
    blurb: "Mistral's audio LLM — high accuracy, 8 languages (no Norwegian). Batch jobs only.",
    langs: "8 languages",
    live: false, // catalog declares live, but build_live_cmd can't launch it (no Voxtral backend wired)
    sizeMlxMB: 1800,
    sizeCpuMB: 2048,
    mlxOk: true,
  },
};

export const FAMILY_ORDER = ["whisper", "nbwhisper", "parakeet", "voxtral"];

/** Always-on runtime extras wired into start.sh AFTER the picker today. The
 *  whole point of surfacing them here is that the CLI hides their cost. */
export const VAD = {
  key: "vad",
  label: "Silence gate",
  blurb: "Per-tap Silero VAD speech gate. Pulls PyTorch (~700 MB).",
  sizeMB: 700,
};
export const SUMMARIZE = {
  key: "summarize",
  label: "Local summarizer",
  blurb: "Offline meeting summaries. Backend now; model on first use.",
  sizeMB: 600,
  modelMB: 4000,
};

/** Pre-baked plans. `recommended` is what variant A pre-selects. */
export const PRESETS = [
  {
    id: "light",
    label: "Light",
    tagline: "Fast English notes",
    families: ["whisper"],
    summarize: false,
  },
  {
    id: "balanced",
    label: "Balanced",
    tagline: "Accurate meetings + summaries",
    families: ["whisper", "parakeet"],
    summarize: true,
    recommended: true,
  },
  {
    id: "best",
    label: "Best quality",
    tagline: "Most accurate, multilingual",
    families: ["whisper", "parakeet", "voxtral"],
    summarize: true,
  },
  {
    id: "everything",
    label: "Everything",
    tagline: "Every family (incl. Norwegian)",
    families: ["whisper", "nbwhisper", "parakeet", "voxtral"],
    summarize: true,
  },
];

/** Variant B speaks in use-cases, not model names. Each maps to a preset. */
// Icons are plain BMP glyphs (present in DejaVu / system-ui everywhere) rather
// than colour emoji, which tofu on headless Chromium and emoji-font-less Linux.
export const USE_CASES = [
  {
    id: "notes",
    icon: "▤",
    title: "Quick notes",
    sub: "Short English voice notes, as fast as possible.",
    preset: "light",
  },
  {
    id: "meetings",
    icon: "▦",
    title: "Team meetings",
    sub: "Accurate transcripts + auto summaries.",
    preset: "balanced",
    recommended: true,
  },
  {
    id: "multi",
    icon: "◐",
    title: "Interviews & multilingual",
    sub: "Highest accuracy across many languages.",
    preset: "best",
  },
  {
    id: "power",
    icon: "▣",
    title: "Install everything",
    sub: "I'll pick models per job later.",
    preset: "everything",
  },
];

// ---- resolution helpers ----------------------------------------------------

/** Pick a backend for a family on a machine, honoring an explicit override. */
export function resolveBackend(machine, fam, override) {
  if (override && override !== "auto") {
    // voxtral has no mlx build — clamp a bad override
    if (override === "mlx" && !fam.mlxOk) return machine.cuda ? "cuda" : "cpu";
    return override;
  }
  if (machine.mlx && fam.mlxOk) return "mlx";
  if (machine.cuda) return "cuda";
  return "cpu";
}

export function backendLabel(b) {
  return { mlx: "MLX", cuda: "CUDA", cpu: "CPU" }[b] || b;
}

export function familySizeMB(fam, backend) {
  if (backend === "mlx" && fam.sizeMlxMB != null) return fam.sizeMlxMB;
  return fam.sizeCpuMB;
}

export function presetById(id) {
  return PRESETS.find((p) => p.id === id);
}

/** preset id -> a selection object the plan builder understands. */
export function selectionFromPreset(id) {
  const p = presetById(id);
  return {
    families: [...p.families],
    summarize: p.summarize,
    backends: {}, // all auto
  };
}

/** Mock "already installed" baseline for the Manage-models context — i.e. what a
 *  prior run installed. (vad is always installed.) Lets the prototype show the
 *  "come back later to add models" entry point: installed rows are kept; only new
 *  picks download. */
export const INSTALLED = { families: ["whisper"], summarize: true };

/** The selection that reflects what's already installed (manage mode seeds this). */
export function installedSelection() {
  return { families: [...INSTALLED.families], summarize: INSTALLED.summarize, backends: {} };
}

// ---- formatting ------------------------------------------------------------

export function fmtSize(mb) {
  if (mb == null) return "—";
  if (mb < 1000) return `${Math.round(mb)} MB`;
  return `${(mb / 1024).toFixed(1)} GB`;
}

const MB_PER_SEC = 9; // rough effective download throughput for the estimate

/** Human est. of download/install time for a byte total. */
export function fmtTime(totalMB, stepCount = 0) {
  const secs = totalMB / MB_PER_SEC + stepCount * 2;
  if (secs < 60) return "< 1 min";
  return `~${Math.round(secs / 60)} min`;
}
