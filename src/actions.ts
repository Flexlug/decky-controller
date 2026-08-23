// UI operations: call the backend, push results into the store, surface errors as toasts.
import { toaster } from "@decky/api";
import * as api from "./api";
import { store } from "./store";
import {
  isApiError,
  looksLikeStatus,
  type Diagnostics,
  type Settings,
  type SettingsPatch,
  type Status,
  type ToastEvent,
} from "./types";

const PLUGIN_TITLE = "Decky Controller";
const TAG = "[decky-controller]";

export function notify(
  body: string,
  severity: ToastEvent["severity"] = "info",
  title: string = PLUGIN_TITLE,
): void {
  toaster.toast({
    title,
    body,
    duration: severity === "info" ? 5000 : 8000,
  });
}

function errorText(e: unknown): string {
  if (e instanceof Error) return e.message;
  return typeof e === "string" ? e : JSON.stringify(e);
}

/** Returns true when a Status was applied; `context` prefixes the error toast. */
function applyStatusResult(result: Status | unknown, context: string): boolean {
  if (isApiError(result)) {
    notify(`${context}: ${result.error}`, "error");
    return false;
  }
  if (looksLikeStatus(result)) {
    store.setStatus(result);
    return true;
  }
  notify(`${context}: unexpected response from backend`, "error");
  console.error(TAG, context, "unexpected response", result);
  return false;
}

/** Silent on transport errors — used by polling. */
export async function refreshStatus(): Promise<void> {
  try {
    const result = await api.getStatus();
    if (looksLikeStatus(result)) {
      store.setStatus(result);
    } else if (isApiError(result)) {
      console.warn(TAG, "get_status error:", result.error);
    }
  } catch (e) {
    console.warn(TAG, "get_status failed:", errorText(e));
  }
}

export async function loadSettings(): Promise<void> {
  try {
    const result = await api.getSettings();
    if (isApiError(result)) {
      notify(`Could not load settings: ${result.error}`, "error");
      return;
    }
    store.setSettings(result as Settings);
  } catch (e) {
    notify(`Could not load settings: ${errorText(e)}`, "error");
  }
}

/** Optimistic local update; the backend's merged result wins, previous settings are restored on failure. */
export async function updateSettings(patch: SettingsPatch): Promise<void> {
  const previous = store.getSettings();
  store.setSettings({
    ...previous,
    ...patch,
    paddles: { ...previous.paddles, ...(patch.paddles ?? {}) },
  });
  try {
    const result = await api.setSettings(patch);
    if (isApiError(result)) {
      store.setSettings(previous);
      notify(`Could not save settings: ${result.error}`, "error");
      return;
    }
    store.setSettings(result as Settings);
  } catch (e) {
    store.setSettings(previous);
    notify(`Could not save settings: ${errorText(e)}`, "error");
  }
}

export async function startSession(): Promise<void> {
  if (store.isBusy()) return;
  store.setBusy(true);
  try {
    const result = await api.start(store.getSettings().profile);
    applyStatusResult(result, "Start failed");
  } catch (e) {
    notify(`Start failed: ${errorText(e)}`, "error");
  } finally {
    store.setBusy(false);
  }
}

/** Idempotent full rollback; safe in any state. */
export async function stopSession(): Promise<void> {
  if (store.isBusy()) return;
  store.setBusy(true);
  try {
    const result = await api.stop();
    applyStatusResult(result, "Stop failed");
  } catch (e) {
    notify(`Stop failed: ${errorText(e)}`, "error");
  } finally {
    store.setBusy(false);
  }
}

/** null when the call failed (an error toast has been shown). */
export async function loadDiagnostics(): Promise<Diagnostics | null> {
  try {
    const result = await api.getDiagnostics();
    if (isApiError(result)) {
      notify(`Diagnostics failed: ${result.error}`, "error");
      return null;
    }
    return result;
  } catch (e) {
    notify(`Diagnostics failed: ${errorText(e)}`, "error");
    return null;
  }
}

export function onStatusEvent(status: unknown): void {
  if (looksLikeStatus(status)) {
    store.setStatus(status);
  } else {
    console.warn(TAG, "ignoring malformed status event", status);
  }
}

export function onToastEvent(event: unknown): void {
  const toast = (event ?? {}) as Partial<ToastEvent>;
  const severity: ToastEvent["severity"] =
    toast.severity === "error" || toast.severity === "warn" ? toast.severity : "info";
  notify(String(toast.body ?? ""), severity, toast.title ? String(toast.title) : PLUGIN_TITLE);
}
