// Backend callables. Names and argument lists are part of the backend contract — do not rename.
import { callable } from "@decky/api";
import type {
  ApiError,
  Diagnostics,
  Profile,
  Settings,
  SettingsPatch,
  Status,
} from "./types";

export const getStatus = callable<[], Status | ApiError>("get_status");

export const start = callable<[profile: Profile], Status | ApiError>("start");

export const stop = callable<[], Status | ApiError>("stop");

export const getSettings = callable<[], Settings | ApiError>("get_settings");

export const setSettings = callable<[settings: SettingsPatch], Settings | ApiError>(
  "set_settings",
);

export const getDiagnostics = callable<[], Diagnostics | ApiError>("get_diagnostics");
