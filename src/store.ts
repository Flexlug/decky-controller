// Module-level store: the QAM panel unmounts whenever the menu closes, but the plugin keeps receiving
// `status` events for the whole session, so the panel, the ACTIVE modal and the listeners share this state.
import { useEffect, useState } from "react";
import { DEFAULT_SETTINGS, type Settings, type Status } from "./types";

type Listener = () => void;

interface StoreState {
  status: Status | null;
  settings: Settings;
  settingsLoaded: boolean;
  /** start/stop request in flight — disables the toggle/buttons. */
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

export function useStore(): StoreState {
  const [, bump] = useState(0);
  useEffect(() => store.subscribe(() => bump((count) => count + 1)), []);
  return state;
}
