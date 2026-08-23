import {
  ButtonItem,
  DropdownItem,
  Field,
  PanelSection,
  PanelSectionRow,
  ToggleField,
  showModal,
  type SingleDropdownOption,
} from "@decky/ui";
import { useEffect, type FC, type ReactNode } from "react";
import { FaCircle } from "react-icons/fa";
import {
  loadDiagnostics,
  loadSettings,
  refreshStatus,
  startSession,
  stopSession,
  updateSettings,
} from "./actions";
import { DiagnosticsModal, DrdHelpModal } from "./modals";
import { useStore } from "./store";
import {
  KILL_COMBO_LABELS,
  PADDLES,
  PADDLE_ACTION_LABELS,
  PROFILE_DESCRIPTIONS,
  PROFILE_LABELS,
  PROFILE_NOUNS,
  SESSION_STATE_DESCRIPTIONS,
  SESSION_STATE_LABELS,
  type KillCombo,
  type Paddle,
  type PaddleAction,
  type Profile,
  type Status,
} from "./types";

/** Poll while the panel is open — cable/DRD change while idle. */

type Tone = "good" | "warn" | "bad" | "off";
const TONE_COLOR: Record<Tone, string> = {
  good: "#59bf40",
  warn: "#e5a23a",
  bad: "#d94126",
  off: "#7a8088",
};

const dropdownOptions = <T extends string>(labels: Record<T, string>): SingleDropdownOption[] =>
  (Object.keys(labels) as T[]).map((data) => ({ data, label: labels[data] }));

const PROFILE_OPTIONS = dropdownOptions(PROFILE_LABELS);
const KILL_COMBO_OPTIONS = dropdownOptions(KILL_COMBO_LABELS);
const PADDLE_OPTIONS = dropdownOptions(PADDLE_ACTION_LABELS);

/** Status line: label left, dot + value right; `shift-children-below` keeps a long value inside the panel. */
const StatusRow: FC<{ label: string; value: ReactNode; tone: Tone; description?: ReactNode }> = ({
  label,
  value,
  tone,
  description,
}) => (
  <PanelSectionRow>
    <Field
      label={label}
      description={description}
      childrenContainerWidth="min"
      inlineWrap="shift-children-below"
      bottomSeparator="standard"
    >
      <span
        style={{
          display: "inline-flex",
          alignItems: "center",
          justifyContent: "flex-end",
          gap: 7,
          maxWidth: "100%",
          textAlign: "right",
        }}
      >
        <FaCircle size={9} color={TONE_COLOR[tone]} style={{ flexShrink: 0 }} />
        <span style={{ minWidth: 0, overflowWrap: "anywhere" }}>{value}</span>
      </span>
    </Field>
  </PanelSectionRow>
);

interface RowInfo {
  value: string;
  tone: Tone;
  description?: string;
}

function drdRow(status: Status): RowInfo {
  return status.drd_enabled
    ? { value: "Enabled", tone: "good" }
    : {
        value: "Disabled",
        tone: "bad",
        description: "Enable USB Dual-Role Device in the BIOS — see “How to enable DRD” below.",
      };
}

/** States in which our gadget is bound, so `udc_state` / `host_connected` mean something. */
const GADGET_BOUND_STATES: ReadonlySet<Status["session_state"]> = new Set(["GADGET_UP", "WAITING_HOST", "ACTIVE"]);

const PLUG_INTO_PC = "Plug the Deck into a PC with a USB-C data cable.";

/** "Host" + enumeration while the gadget is bound; otherwise what the port physically sees (`cable_kind`).
 *  Values <= 14 characters, never volts/amps/"PD contract". */
function cableRow(status: Status): RowInfo & { label: string } {
  if (!status.drd_enabled) return { label: "Cable", value: "N/A", tone: "off", description: "Requires DRD." };
  if (GADGET_BOUND_STATES.has(status.session_state)) {
    if (status.host_connected) {
      return { label: "Host", value: "Connected", tone: "good", description: "The PC sees the controller." };
    }
    return {
      label: "Host",
      value: "Waiting…",
      tone: "warn",
      description: "Waiting for the PC to enumerate the controller.",
    };
  }
  switch (status.cable_kind) {
    case "none":
      return { label: "Cable", value: "Not connected", tone: "off", description: PLUG_INTO_PC };
    case "pc":
      return { label: "Cable", value: "PC", tone: "good", description: "Plugged into a PC. Ready to start." };
    case "charger":
      return {
        label: "Cable",
        value: "Charger",
        tone: "warn",
        description: "Only a charger is connected — plug into a PC.",
      };
    case "host_device":
      return {
        label: "Cable",
        value: "Dock",
        tone: "warn",
        description: "A dock/accessory is attached — unplug it and connect the Deck to a PC.",
      };
    case "unknown":
      return {
        label: "Cable",
        value: "Unknown",
        tone: "warn",
        description: "Power on the port; waiting for details.",
      };
    default:
      break;
  }
  // older backend without cable_kind
  if (status.host_connected) {
    return { label: "Cable", value: "PC", tone: "good", description: "Plugged into a PC. Ready to start." };
  }
  if ((status.extcon?.["USB-HOST"] ?? 0) === 1) {
    return {
      label: "Cable",
      value: "Dock",
      tone: "warn",
      description: "A dock/accessory is attached — unplug it and connect the Deck to a PC.",
    };
  }
  return { label: "Cable", value: "Not connected", tone: "off", description: PLUG_INTO_PC };
}

function controllerRow(status: Status): RowInfo {
  if (!status.neptune_present) {
    return { value: "Not found", tone: "bad", description: "Built-in controller (28de:1205) not detected." };
  }
  return status.neptune_captured
    ? { value: "Captured", tone: "warn", description: "Input goes to the PC, not to Steam." }
    : { value: "Steam", tone: "good" };
}

function modeRow(status: Status): RowInfo {
  const label = SESSION_STATE_LABELS[status.session_state] ?? status.session_state;
  if (status.session_state === "ACTIVE") {
    const profile = status.active_profile ? PROFILE_LABELS[status.active_profile] : "";
    // whole number keeps the value <= 14 chars
    const hz = status.metrics?.hz ? ` · ${Math.round(status.metrics.hz)}Hz` : "";
    return { value: `${label}${hz}`, tone: "good", description: `${profile}${status.transport ? ` via ${status.transport}` : ""}` };
  }
  if (status.session_state === "IDLE") return { value: label, tone: "off" };
  return { value: label, tone: "warn" };
}

export const Content: FC = () => {
  const { status, settings, settingsLoaded, busy } = useStore();

  useEffect(() => {
    void refreshStatus();   // the backend pushes every later change as a `status` event
    void loadSettings();
  }, []);

  const running = !!status && status.session_state !== "IDLE";
  const canStart = !!status && status.drd_enabled && status.neptune_present;
  const toggleDisabled = busy || !status || (!running && !canStart);

  let toggleDescription: string;
  if (!status) toggleDescription = "Connecting to backend…";
  else if (running)
    toggleDescription =
      SESSION_STATE_DESCRIPTIONS[status.session_state] || SESSION_STATE_LABELS[status.session_state];
  else if (!status.drd_enabled) toggleDescription = "DRD is disabled in the BIOS.";
  else if (!status.neptune_present) toggleDescription = "Built-in controller not found.";
  else if (status.cable_kind === "pc" || status.host_connected)
    toggleDescription = `Start as ${PROFILE_NOUNS[settings.profile]}.`;
  else if (status.cable_kind === "charger")
    toggleDescription = "Only a charger is connected — plug the Deck into a PC (you can start now and re-plug later).";
  else if (status.cable_kind === "host_device")
    toggleDescription = "Unplug the dock/accessory first — the port is in host mode.";
  else toggleDescription = "You can start now and plug in the cable later.";

  const holdSeconds = (settings.kill_hold_ms / 1000).toFixed(1).replace(/\.0$/, "");

  const openDiagnostics = async (): Promise<void> => {
    const data = await loadDiagnostics();
    if (data) showModal(<DiagnosticsModal data={data} />);
  };

  return (
    <>
      <PanelSection title="Controller mode">
        <PanelSectionRow>
          <ToggleField
            label="Controller mode"
            description={toggleDescription}
            checked={running}
            disabled={toggleDisabled}
            onChange={(on: boolean) => void (on ? startSession() : stopSession())}
          />
        </PanelSectionRow>
      </PanelSection>

      <PanelSection title="Status" spinner={!status}>
        {status && (
          <>
            <StatusRow label="DRD" {...drdRow(status)} />
            <StatusRow {...cableRow(status)} />
            <StatusRow label="Controller" {...controllerRow(status)} />
            <StatusRow label="Mode" {...modeRow(status)} />
            {status.last_error && (
              <PanelSectionRow>
                <Field label="Last error" description={status.last_error} bottomSeparator="standard" />
              </PanelSectionRow>
            )}
          </>
        )}
      </PanelSection>

      <PanelSection title="Settings">
        <PanelSectionRow>
          <DropdownItem
            label="Profile"
            description={`${PROFILE_DESCRIPTIONS[settings.profile]}${running ? " Applies on the next start." : ""}`}
            rgOptions={PROFILE_OPTIONS}
            selectedOption={settings.profile}
            disabled={!settingsLoaded}
            onChange={(option: SingleDropdownOption) => void updateSettings({ profile: option.data as Profile })}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <DropdownItem
            label="Kill switch"
            description={`Hold for ${holdSeconds} s to exit controller mode. Never forwarded to the PC.`}
            rgOptions={KILL_COMBO_OPTIONS}
            selectedOption={settings.kill_combo}
            disabled={!settingsLoaded}
            onChange={(option: SingleDropdownOption) => void updateSettings({ kill_combo: option.data as KillCombo })}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <ToggleField
            label="Turn screen off while active"
            description={`Touch the screen to wake it for ${settings.touch_wake_seconds} s.`}
            checked={settings.screen_off}
            disabled={!settingsLoaded}
            onChange={(on: boolean) => void updateSettings({ screen_off: on })}
          />
        </PanelSectionRow>
      </PanelSection>

      <PanelSection title="Back paddles">
        {PADDLES.map((paddle: Paddle) => (
          <PanelSectionRow key={paddle}>
            <DropdownItem
              label={paddle}
              rgOptions={PADDLE_OPTIONS}
              selectedOption={settings.paddles[paddle]}
              disabled={!settingsLoaded}
              onChange={(option: SingleDropdownOption) =>
                void updateSettings({ paddles: { [paddle]: option.data as PaddleAction } })
              }
            />
          </PanelSectionRow>
        ))}
      </PanelSection>

      <PanelSection title="Tools">
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={() => showModal(<DrdHelpModal />)}>
            How to enable DRD
          </ButtonItem>
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem layout="below" disabled={busy} onClick={() => void stopSession()}>
            Stop (full reset)
          </ButtonItem>
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={() => void openDiagnostics()}>
            Diagnostics
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>
    </>
  );
};
