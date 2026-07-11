// Ambient types for the SpatialChat Bridge popup module.
//
// control-client.js is a classic global IIFE (the content script needs it as a
// global in the isolated world, and MV3 content scripts can't be ES modules),
// so the module popup reads `TapscribeControlClient` as a global rather than
// importing it. Declared here to type that global + the minimal chrome.* surface
// the popup uses (the gate runs with `types: []`, so no @types/chrome).

// `RecorderCfg` is the global typedef control-client.js (a classic script)
// already contributes — reused here so the two can't drift.

interface ControlClient {
  createDetachedSession(
    cfg: RecorderCfg,
    opts?: { timeoutMs?: number },
  ): Promise<{ sessionId: string; path: unknown }>;
  pollPipeline(cfg: RecorderCfg, sessionId: string, opts?: { timeoutMs?: number }): Promise<any>;
  triggerPipeline(cfg: RecorderCfg, sessionId: string, opts?: { timeoutMs?: number }): Promise<{ outcome: string }>;
  checkHealth(
    cfg: RecorderCfg,
    opts?: { timeoutMs?: number },
  ): Promise<{ ok: boolean; status?: number; error?: string; body?: any; url: string }>;
  probeTapToken(cfg: RecorderCfg, opts?: { timeoutMs?: number }): Promise<{ ok: boolean; error?: string }>;
  httpBase(cfg: RecorderCfg): string;
  isTrustworthyHost(host: string | null | undefined): boolean;
}

declare const TapscribeControlClient: ControlClient;

interface StorageChange {
  newValue?: any;
  oldValue?: any;
}

type StorageListener = (changes: Record<string, StorageChange>, areaName: string) => void;

declare const chrome: {
  storage: {
    local: {
      get(keys: string[]): Promise<Record<string, any>>;
      set(items: Record<string, any>): Promise<void>;
    };
    onChanged: {
      addListener(cb: StorageListener): void;
      removeListener(cb: StorageListener): void;
    };
  };
  tabs: { create(props: { url: string }): void };
};
