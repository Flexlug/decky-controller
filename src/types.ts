/**
 * Shared types for the Decky Controller frontend.
 *
 * The shapes below mirror docs/ARCHITECTURE.md (backend callables, Status, Settings)
 * exactly; any field added here must also exist in the backend contract.
 */

/** Session state machine of the deckgadget daemon (docs/ARCHITECTURE.md). */
export type SessionState =
  | "IDLE"
  | "CAPTURING"
  | "GADGET_UP"
  | "WAITING_HOST"
  | "ACTIVE"
  | "STOPPING";

/** Emulated device profile. */
export type Profile = "xbox360" | "hid_gamepad";

/** USB transport ("auto" = raw for xbox360, hid for hid_gamepad). */
export type TransportSetting = "auto" | "raw" | "hid";
export type ActiveTransport = "raw" | "hid";

/** Hardware combo that the daemon treats as the kill switch. */
export type KillCombo = "L4+R4" | "L5+R5" | "L4+L5+R4+R5" | "STEAM+QAM";

/** Back paddle identifiers. */
export type Paddle = "L4" | "L5" | "R4" | "R5";

/** What a back paddle is forwarded to the PC as. */
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

/** Error envelope returned by every backend callable instead of throwing. */
export interface ApiError {
  ok: false;
  error: string;
}

/**
 * What the USB-C port physically sees, independent of whether a gadget is bound (docs/ARCHITECTURE.md).
 * `host_device` = dock/peripheral attached (port is a host); `none` = no power on the port;
 * `pc` = a plain PC/hub port; `charger` = a PD charger; `unknown` = unreadable.
 * (The backend classifies by the negotiated PD contract; the UI never shows volts/amps.)
 */
export type CableKind = "none" | "pc" | "charger" | "host_device" | "unknown";

/** `Status` dict (docs/ARCHITECTURE.md). */
export interface Status {
  ok: true;
  plugin_version: string;
  kernel: string | null;
  model: string | null;
  drd_enabled: boolean;
  udc_name: string | null;
  udc_state: string | null;
  extcon: Record<string, number>;
  /** A host enumerated our gadget (`udc_state === "configured"`); only meaningful once a session is up. */
  host_connected: boolean;
  /** Power on the USB-C port (`/sys/class/power_supply/ACAD/online`); null/absent = unreadable. */
  cable_power?: boolean | null;
  /** Negotiated USB-PD contract (steamdeck_hwmon), millivolts / milliamps; null/absent = unreadable. */
  pd_contract_mv?: number | null;
  pd_contract_ma?: number | null;
  /** Classification of the above; absent on an older backend. */
  cable_kind?: CableKind;
  neptune_present: boolean;
  neptune_captured: boolean;
  daemon_running: boolean;
  session_state: SessionState;
  active_profile: Profile | null;
  transport: ActiveTransport | null;
  screen_off: boolean;
  last_error: string | null;
  metrics: { hz: number; reports: number };
}

/** `Settings` dict (docs/ARCHITECTURE.md). */
export interface Settings {
  profile: Profile;
  transport: TransportSetting;
  kill_combo: KillCombo;
  kill_hold_ms: number;
  screen_off: boolean;
  touch_wake_seconds: number;
  paddles: Record<Paddle, PaddleAction>;
}

/** Partial settings accepted by `set_settings` (nested `paddles` may be partial too). */
export type SettingsPatch = Partial<Omit<Settings, "paddles">> & {
  paddles?: Partial<Record<Paddle, PaddleAction>>;
};

/** Payload of the backend `toast` event. */
export interface ToastEvent {
  title: string;
  body: string;
  severity: "info" | "warn" | "error";
}

/** Raw diagnostics blob from `get_diagnostics` (shape is backend-defined; shown verbatim). */
export type Diagnostics = Record<string, unknown>;

/** Default settings, used until the backend answers `get_settings`. */
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

/*
 * UI text budget (the QAM panel is ~400 px wide): dropdown option labels and status-row values
 * must stay <= 14 characters (options render inside a ~150 px button); descriptions may be
 * longer - they wrap.
 */

/** Short names used in the Profile dropdown and the Mode row (<= 14 chars). */
export const PROFILE_LABELS: Record<Profile, string> = {
  xbox360: "Xbox 360",
  hid_gamepad: "Generic HID",
};

/** Noun phrase for sentences ("Start as …", "…is now …"). */
export const PROFILE_NOUNS: Record<Profile, string> = {
  xbox360: "an Xbox 360 controller (XInput)",
  hid_gamepad: "a generic HID gamepad",
};

/** One-line help shown under the Profile dropdown. */
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

/** Short value for the Mode row (<= 14 chars). */
export const SESSION_STATE_LABELS: Record<SessionState, string> = {
  IDLE: "Idle",
  CAPTURING: "Capturing…",
  GADGET_UP: "Starting…",
  WAITING_HOST: "Waiting…",
  ACTIVE: "Active",
  STOPPING: "Stopping…",
};

/** Longer one-liner for the Controller-mode toggle while a session is running. */
export const SESSION_STATE_DESCRIPTIONS: Record<SessionState, string> = {
  IDLE: "",
  CAPTURING: "Taking over the built-in controller…",
  GADGET_UP: "Bringing up the USB gadget…",
  WAITING_HOST: "Waiting for the PC to enumerate the controller…",
  ACTIVE: "Active — input goes to the PC.",
  STOPPING: "Stopping and restoring the controller…",
};

/** Type guard for the `{ok:false,error}` envelope. */
export function isApiError(value: unknown): value is ApiError {
  return (
    typeof value === "object" &&
    value !== null &&
    (value as { ok?: unknown }).ok === false
  );
}

/** Loose runtime check so a malformed `status` event cannot poison the UI. */
export function looksLikeStatus(value: unknown): value is Status {
  return (
    typeof value === "object" &&
    value !== null &&
    typeof (value as { session_state?: unknown }).session_state === "string"
  );
}
