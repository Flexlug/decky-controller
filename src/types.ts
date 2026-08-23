// Shapes of the backend contract (Status, Settings, events) — keys and values must match main.py.

export type SessionState =
  | "IDLE"
  | "CAPTURING"
  | "GADGET_UP"
  | "WAITING_HOST"
  | "ACTIVE"
  | "STOPPING";

export type Profile = "xbox360" | "hid_gamepad";

/** "auto" = raw for xbox360, hid for hid_gamepad. */
export type TransportSetting = "auto" | "raw" | "hid";
export type ActiveTransport = "raw" | "hid";

export type KillCombo = "L4+R4" | "L5+R5" | "L4+L5+R4+R5" | "STEAM+QAM";

export type Paddle = "L4" | "L5" | "R4" | "R5";

export type PaddleAction =
  | "none"
  | "A"
  | "B"
  | "X"
  | "Y"
  | "LB"
  | "RB"
  | "L3"
  | "R3"
  | "VIEW"
  | "MENU"
  | "DPAD_UP"
  | "DPAD_DOWN"
  | "DPAD_LEFT"
  | "DPAD_RIGHT";

/** Returned by every backend callable instead of throwing. */
export interface ApiError {
  ok: false;
  error: string;
}

/** What the USB-C port physically sees while idle: host_device = dock/peripheral, none = no power, unknown = unreadable. */
export type CableKind = "none" | "pc" | "charger" | "host_device" | "unknown";

export interface Status {
  ok: true;
  plugin_version: string;
  kernel: string | null;
  model: string | null;
  drd_enabled: boolean;
  udc_name: string | null;
  udc_state: string | null;
  udc_speed: string | null;
  extcon: Record<string, number>;
  /** udc_state === "configured" — only meaningful once a session is up. */
  host_connected: boolean;
  // cable_* describe the port independent of the gadget; null/absent = unreadable. Never shown as volts/amps.
  cable_power?: boolean | null;
  pd_contract_mv?: number | null;
  pd_contract_ma?: number | null;
  cable_kind?: CableKind;
  neptune_present: boolean;
  neptune_captured: boolean;
  daemon_running: boolean;
  daemon_pid: number | null;
  session_state: SessionState;
  session_detail: string;
  active_profile: Profile | null;
  transport: ActiveTransport | null;
  screen_off: boolean;
  last_error: string | null;
  metrics: { hz: number; reports: number; dropped: number };
}

export interface Settings {
  profile: Profile;
  transport: TransportSetting;
  kill_combo: KillCombo;
  kill_hold_ms: number;
  screen_off: boolean;
  touch_wake_seconds: number;
  paddles: Record<Paddle, PaddleAction>;
}

/** Accepted by `set_settings`; `paddles` may be partial too. */
export type SettingsPatch = Partial<Omit<Settings, "paddles">> & {
  paddles?: Partial<Record<Paddle, PaddleAction>>;
};

export interface ToastEvent {
  title: string;
  body: string;
  severity: "info" | "warn" | "error";
}

/** Backend-defined blob from `get_diagnostics`, shown verbatim. */
export type Diagnostics = Record<string, unknown>;

/** Used until the backend answers `get_settings`. */
export const DEFAULT_SETTINGS: Settings = {
  profile: "xbox360",
  transport: "auto",
  kill_combo: "L4+R4",
  kill_hold_ms: 1500,
  screen_off: true,
  touch_wake_seconds: 5,
  paddles: { L4: "none", L5: "none", R4: "none", R5: "none" },
};

export const PADDLES: readonly Paddle[] = ["L4", "L5", "R4", "R5"];

// UI text budget: dropdown option labels and status-row values <= 14 characters (the QAM panel is ~400 px
// wide, options render in a ~150 px button); descriptions wrap and may be longer.

export const PROFILE_LABELS: Record<Profile, string> = {
  xbox360: "Xbox 360",
  hid_gamepad: "Generic HID",
};

/** For sentences: "Start as …", "… is now …". */
export const PROFILE_NOUNS: Record<Profile, string> = {
  xbox360: "an Xbox 360 controller (XInput)",
  hid_gamepad: "a generic HID gamepad",
};

export const PROFILE_DESCRIPTIONS: Record<Profile, string> = {
  xbox360: "XInput — works on Windows out of the box.",
  hid_gamepad: "Standard HID gamepad — for Linux hosts or DirectInput-only software.",
};

export const KILL_COMBO_LABELS: Record<KillCombo, string> = {
  "L4+R4": "L4+R4",
  "L5+R5": "L5+R5",
  "L4+L5+R4+R5": "L4+L5+R4+R5",
  "STEAM+QAM": "Steam+QAM",
};

export const PADDLE_ACTION_LABELS: Record<PaddleAction, string> = {
  none: "None",
  A: "A",
  B: "B",
  X: "X",
  Y: "Y",
  LB: "LB",
  RB: "RB",
  L3: "L3 (stick)",
  R3: "R3 (stick)",
  VIEW: "View (Back)",
  MENU: "Menu (Start)",
  DPAD_UP: "D-pad Up",
  DPAD_DOWN: "D-pad Down",
  DPAD_LEFT: "D-pad Left",
  DPAD_RIGHT: "D-pad Right",
};

export const SESSION_STATE_LABELS: Record<SessionState, string> = {
  IDLE: "Idle",
  CAPTURING: "Capturing…",
  GADGET_UP: "Starting…",
  WAITING_HOST: "Waiting…",
  ACTIVE: "Active",
  STOPPING: "Stopping…",
};

export const SESSION_STATE_DESCRIPTIONS: Record<SessionState, string> = {
  IDLE: "",
  CAPTURING: "Taking over the built-in controller…",
  GADGET_UP: "Bringing up the USB gadget…",
  WAITING_HOST: "Waiting for the PC to enumerate the controller…",
  ACTIVE: "Active — input goes to the PC.",
  STOPPING: "Stopping and restoring the controller…",
};

export function isApiError(value: unknown): value is ApiError {
  return (
    typeof value === "object" &&
    value !== null &&
    (value as { ok?: unknown }).ok === false
  );
}

/** Loose check so a malformed `status` event cannot poison the UI. */
export function looksLikeStatus(value: unknown): value is Status {
  return (
    typeof value === "object" &&
    value !== null &&
    typeof (value as { session_state?: unknown }).session_state === "string"
  );
}
