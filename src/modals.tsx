/**
 * Modal dialogs: ACTIVE-session overlay, BIOS/DRD instructions and raw diagnostics.
 *
 * All modals are opened with `showModal(<X />)`; Steam injects a `closeModal` prop
 * into the root element, which we forward to `ModalRoot`.
 */
import {
  DialogBody,
  DialogBodyText,
  DialogButton,
  DialogFooter,
  DialogHeader,
  Focusable,
  ModalRoot,
} from "@decky/ui";
import type { CSSProperties, FC } from "react";
import { stopSession } from "./actions";
import { useStore } from "./store";
import {
  KILL_COMBO_LABELS,
  PROFILE_NOUNS,
  type Diagnostics,
} from "./types";

interface ModalProps {
  /** Injected by showModal(); closes this modal. */
  closeModal?: () => void;
}

const holdSeconds = (ms: number): string => (ms / 1000).toFixed(1).replace(/\.0$/, "");

/**
 * Shown while session_state === "ACTIVE". The built-in controller is captured by the
 * daemon, so this dialog is touch-only: the screen wakes for a few seconds on touch
 * and the big Stop button is the on-screen way out besides the kill combo.
 */
export const ActiveModal: FC<ModalProps> = ({ closeModal }) => {
  const { status, settings, busy } = useStore();
  const profile = status?.active_profile ?? settings.profile;
  const hz = status?.metrics?.hz ?? 0;

  return (
    <ModalRoot
      closeModal={closeModal}
      onCancel={closeModal}
      bDisableBackgroundDismiss
      bHideCloseIcon
    >
      <DialogHeader>Controller mode is active</DialogHeader>
      <DialogBody>
        <DialogBodyText>
          Your Steam Deck is now <b>{PROFILE_NOUNS[profile]}</b> for the connected PC.
          Steam does not receive any controller input while this mode is on.
        </DialogBodyText>
        <DialogBodyText>
          Hold <b>{KILL_COMBO_LABELS[settings.kill_combo]}</b> for{" "}
          <b>{holdSeconds(settings.kill_hold_ms)} s</b> to exit, or tap Stop below.
          {settings.screen_off && " The screen turns off to save power — touch it to wake."}
        </DialogBodyText>
        <DialogBodyText style={{ opacity: 0.7 }}>
          {hz > 0 ? `${hz} reports/s` : "Waiting for the first reports…"}
          {status?.transport ? ` · transport: ${status.transport}` : ""}
        </DialogBodyText>
      </DialogBody>
      <DialogFooter>
        <Focusable style={{ display: "flex", justifyContent: "flex-end", gap: 12 }}>
          <DialogButton
            disabled={busy}
            onClick={() => void stopSession()}
            style={{ minWidth: 220, fontSize: 18 }}
          >
            {busy ? "Stopping…" : "Stop controller mode"}
          </DialogButton>
        </Focusable>
      </DialogFooter>
    </ModalRoot>
  );
};

const STEP_LIST: CSSProperties = { margin: "8px 0 0 18px", lineHeight: 1.6 };

/** BIOS instructions for enabling USB Dual-Role Device (DRD). */
export const DrdHelpModal: FC<ModalProps> = ({ closeModal }) => (
  <ModalRoot closeModal={closeModal} onCancel={closeModal}>
    <DialogHeader>How to enable DRD (USB Dual-Role Device)</DialogHeader>
    <DialogBody>
      <DialogBodyText>
        The USB-C port can only act as a device (gamepad) when the BIOS exposes the
        controller in Dual-Role mode. This is a one-time change and can be reverted the
        same way.
      </DialogBodyText>
      <DialogBodyText>
        <ol style={STEP_LIST}>
          <li>Power off the Steam Deck completely.</li>
          <li>
            Hold <b>Volume Up (+)</b> and press <b>Power</b>; release both when you hear
            the chime.
          </li>
          <li>
            Choose <b>Setup Utility</b> → <b>Advanced</b> → <b>USB Configuration</b>.
          </li>
          <li>
            Set <b>USB Dual-Role Device</b> from <b>XHCI</b> to <b>DRD</b>.
          </li>
          <li>Save and exit (Exit tab → Exit Saving Changes). The Deck reboots.</li>
          <li>
            Connect the Deck to the PC with a <b>USB-C data cable</b> (directly, not via
            a dock) and turn on Controller mode.
          </li>
        </ol>
      </DialogBodyText>
      <DialogBodyText style={{ opacity: 0.7 }}>
        Docks and USB accessories keep working normally - the port switches role
        automatically depending on what is plugged in.
      </DialogBodyText>
    </DialogBody>
    <DialogFooter>
      <Focusable style={{ display: "flex", justifyContent: "flex-end" }}>
        <DialogButton onClick={closeModal} style={{ minWidth: 160 }}>
          Close
        </DialogButton>
      </Focusable>
    </DialogFooter>
  </ModalRoot>
);

const PRE_STYLE: CSSProperties = {
  maxHeight: "55vh",
  overflow: "auto",
  fontSize: 12,
  lineHeight: 1.35,
  whiteSpace: "pre-wrap",
  wordBreak: "break-all",
  background: "rgba(0,0,0,0.35)",
  padding: 10,
  borderRadius: 4,
  userSelect: "text",
};

/** Raw output of `get_diagnostics` (shape is backend-defined, printed as JSON). */
export const DiagnosticsModal: FC<ModalProps & { data: Diagnostics }> = ({
  closeModal,
  data,
}) => (
  <ModalRoot closeModal={closeModal} onCancel={closeModal} bAllowFullSize>
    <DialogHeader>Diagnostics</DialogHeader>
    <DialogBody>
      <Focusable style={PRE_STYLE}>
        <pre style={{ margin: 0, fontFamily: "monospace" }}>
          {JSON.stringify(data, null, 2)}
        </pre>
      </Focusable>
    </DialogBody>
    <DialogFooter>
      <Focusable style={{ display: "flex", justifyContent: "flex-end" }}>
        <DialogButton onClick={closeModal} style={{ minWidth: 160 }}>
          Close
        </DialogButton>
      </Focusable>
    </DialogFooter>
  </ModalRoot>
);
