/**
 * Typed wrappers around the backend callables (docs/ARCHITECTURE.md, "Backend callables").
 * Names and argument lists are part of the contract - do not rename.
 */
import { callable } from "@decky/api";
import type {
  ApiError,
  Diagnostics,
  Profile,
  Settings,
  SettingsPatch,
  Status,
} from "./types";

/** `get_status()` -> Status */
export const getStatus = callable<[], Status | ApiError>("get_status");

/** `start(profile)` -> Status after the start attempt */
export const start = callable<[profile: Profile], Status | ApiError>("start");

/** `stop()` -> Status; idempotent full rollback, always safe to call */
export const stop = callable<[], Status | ApiError>("stop");

/** `get_settings()` -> Settings */
export const getSettings = callable<[], Settings | ApiError>("get_settings");

/** `set_settings(settings)` -> merged & persisted Settings */
export const setSettings = callable<[settings: SettingsPatch], Settings | ApiError>(
  "set_settings",
);

/** `get_diagnostics()` -> raw dict (deckgadget status, versions, last log lines) */
export const getDiagnostics = callable<[], Diagnostics | ApiError>("get_diagnostics");
