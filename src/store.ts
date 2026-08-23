/**
 * Tiny module-level store for the latest backend Status / Settings.
 *
 * The QAM panel (Content) unmounts whenever the Quick Access Menu closes, but the
 * plugin itself (definePlugin) lives for the whole session and keeps receiving
 * `status` events. Keeping the state outside React lets the panel, the ACTIVE modal
 * and the event listeners all share one source of truth.
 */
import { useEffect, useState } from "react";
import { DEFAULT_SETTINGS, type Settings, type Status } from "./types";

type Listener = () => void;

interface StoreState {
  status: Status | null;
  settings: Settings;
  settingsLoaded: boolean;
  /** A start/stop request is in flight - disables the toggle/buttons meanwhile. */
  busy: boolean;
}

const state: StoreState = {
  status: null,
  settings: DEFAULT_SETTINGS,
  settingsLoaded: false,
  busy: false,
};

const listeners = new Set<Listener>();

function notify(): void {
  for (const listener of listeners) {
    try {
      listener();
    } catch (e) {
      console.error("[decky-controller] store listener failed", e);
    }
  }
}

export const store = {
  getStatus: (): Status | null => state.status,
  getSettings: (): Settings => state.settings,
  isSettingsLoaded: (): boolean => state.settingsLoaded,
  isBusy: (): boolean => state.busy,

  setStatus(status: Status): void {
    state.status = status;
    notify();
  },
  setSettings(settings: Settings): void {
    state.settings = settings;
    state.settingsLoaded = true;
    notify();
  },
  setBusy(busy: boolean): void {
    if (state.busy === busy) return;
    state.busy = busy;
    notify();
  },
  subscribe(listener: Listener): () => void {
    listeners.add(listener);
    return () => {
      listeners.delete(listener);
    };
  },
};

/** React hook: re-renders the caller whenever the store changes. */
export function useStore(): StoreState {
  const [, bump] = useState(0);
  useEffect(() => store.subscribe(() => bump((n) => n + 1)), []);
  return state;
}
