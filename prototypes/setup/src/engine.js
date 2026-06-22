// Mock install engine. buildPlan() turns a (machine, selection) into an ordered
// list of steps with sizes; runInstall() fakes pip progress + a streaming log so
// every variant can SHOW the thing the CLI hides today. No real installs happen.

import {
  FAMILIES,
  FAMILY_ORDER,
  VAD,
  SUMMARIZE,
  resolveBackend,
  backendLabel,
  familySizeMB,
  fmtSize,
} from "./mock-data.js";

/**
 * @param {object} machine  one of MACHINES
 * @param {{families:string[], summarize:boolean, backends?:object}} selection
 * @param {{manage?:boolean}} [opts] - manage = adding to an existing install:
 *   no venv/secrets/launch (those already exist), no vad (always installed),
 *   and a "reload into the running engine" final step. `selection` is then the
 *   DELTA (only the not-yet-installed picks).
 * @returns {{steps:object[], totalMB:number, machine:object, selection:object}}
 */
export function buildPlan(machine, selection, opts = {}) {
  const manage = !!opts.manage;
  const steps = [];
  if (!manage) steps.push({ id: "venv", label: "Create virtual environment", sizeMB: 12, kind: "env" });

  for (const key of FAMILY_ORDER) {
    if (!selection.families.includes(key)) continue;
    const fam = FAMILIES[key];
    const backend = resolveBackend(machine, fam, selection.backends?.[key]);
    const sizeMB = familySizeMB(fam, backend);
    steps.push({
      id: "fam-" + key,
      famKey: key,
      label: `${fam.label} · ${backendLabel(backend)}`,
      short: fam.label.split(" ")[0],
      sizeMB,
      kind: "model",
      backend,
    });
  }

  // vad is a fresh-install concern only — in manage mode it's already present.
  if (!manage) {
    steps.push({
      id: "vad",
      label: `${VAD.label} · Silero VAD + PyTorch`,
      short: "silero-vad",
      sizeMB: VAD.sizeMB,
      kind: "extra",
    });
  }
  if (selection.summarize) {
    const sb = machine.mlx ? "mlx-lm" : "llama-cpp";
    steps.push({
      id: "summarize",
      label: `${SUMMARIZE.label} · ${sb}`,
      short: sb,
      sizeMB: SUMMARIZE.sizeMB,
      kind: "extra",
      note: "model downloads on first summary",
    });
  }

  if (manage) {
    steps.push({ id: "reload", label: "Reload models into the running engine", sizeMB: 0, kind: "final" });
  } else {
    steps.push({ id: "secrets", label: "Generate dashboard password & tap token", sizeMB: 0, kind: "final" });
    steps.push({ id: "launch", label: "Launch recorder + live channel", sizeMB: 0, kind: "final" });
  }

  const totalMB = steps.reduce((a, s) => a + s.sizeMB, 0);
  return { steps, totalMB, machine, selection };
}

// canned log lines per step (first shown on start, rest as it "progresses")
function stepLogLines(s) {
  if (s.kind === "env") return ["$ python3 -m venv .venv", "  upgrading pip, setuptools, wheel"];
  if (s.id === "secrets") return ["  writing .auth-password", "  writing .tap-token"];
  if (s.id === "launch")
    return ["  recorder listening on :8001", "  spawning whisperlivekit-server child", "  live channel ready"];
  return [
    `  resolving wheels for ${s.short}`,
    `  downloading ${s.short} (${fmtSize(s.sizeMB)})`,
    `  installed ${s.short}`,
  ];
}

const WORDS = [
  "amber", "otter", "truss", "harbor", "quartz", "willow", "cobalt",
  "marsh", "ember", "tundra", "vellum", "nimbus", "fjord", "slate",
];

function makeSecrets() {
  const pick = () => WORDS[Math.floor(Math.random() * WORDS.length)];
  const password = `${pick()}-${pick()}-${pick()}-${1000 + Math.floor(Math.random() * 9000)}`;
  let token = "";
  const hex = "0123456789abcdef";
  for (let i = 0; i < 40; i++) token += hex[Math.floor(Math.random() * 16)];
  return { password, token, url: "http://localhost:8001/" };
}

/**
 * Drive a plan to completion with watchable fake progress.
 * Mutates each step's `pct` (0..100) and `state` ('pending'|'active'|'done').
 * @returns {() => void} cancel fn
 */
export function runInstall(plan, { onTick, onLog, onDone }) {
  const steps = plan.steps;
  // weight: heavier steps take proportionally longer (sqrt keeps torch from
  // eating the whole bar), env/final are quick fixed beats.
  for (const s of steps) {
    s.pct = 0;
    s.state = "pending";
    s._w = s.kind === "final" || s.kind === "env" ? 5 : Math.max(8, Math.sqrt(s.sizeMB) * 4);
    s._logged = 0;
  }
  const totalW = steps.reduce((a, s) => a + s._w, 0);
  const DURATION = 9000;
  const TICK = 90;
  let elapsed = 0;

  const timer = setInterval(() => {
    elapsed += TICK;
    const targetW = (elapsed / DURATION) * totalW;
    let acc = 0;
    for (const s of steps) {
      const start = acc;
      const end = acc + s._w;
      acc = end;
      const prev = s.state;
      if (targetW <= start) {
        s.pct = 0;
        s.state = "pending";
      } else if (targetW >= end) {
        s.pct = 100;
        s.state = "done";
      } else {
        s.pct = Math.round(((targetW - start) / s._w) * 100);
        s.state = "active";
      }
      // stream log lines as the step advances
      if (s.state === "active" || s.state === "done") {
        const lines = stepLogLines(s);
        const want = s.state === "done" ? lines.length : Math.min(lines.length - 1, 1 + Math.floor(s.pct / 60));
        while (s._logged < want) onLog(lines[s._logged++]);
      }
      if (prev !== "done" && s.state === "done" && s.kind !== "model" && s.kind !== "extra") {
        // env/final lines already emitted above
      }
    }
    const overall = Math.min(100, Math.round((targetW / totalW) * 100));
    onTick(overall);
    if (elapsed >= DURATION) {
      clearInterval(timer);
      for (const s of steps) {
        s.pct = 100;
        s.state = "done";
      }
      onTick(100);
      onDone(makeSecrets());
    }
  }, TICK);

  return () => clearInterval(timer);
}
