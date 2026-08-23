import { showModal, staticClasses, type ShowModalResult } from "@decky/ui";
import { addEventListener, definePlugin, removeEventListener } from "@decky/api";
import { FaGamepad } from "react-icons/fa";
import { onStatusEvent, onToastEvent, refreshStatus } from "./actions";
import { Content } from "./Content";
import { ActiveModal } from "./modals";
import { store } from "./store";
import type { Status, ToastEvent } from "./types";

const PLUGIN_NAME = "Decky Controller";

export default definePlugin(() => {
  console.log("[decky-controller] frontend init");

  let activeModal: ShowModalResult | null = null;
  let wasActive = false;

  const closeActiveModal = (): void => {
    const handle = activeModal;
    activeModal = null;
    try {
      handle?.Close();
    } catch (e) {
      console.warn("[decky-controller] closing ACTIVE modal failed", e);
    }
  };

  // The store is updated by both `status` events and callable results, so this covers every path.
  const unsubscribe = store.subscribe(() => {
    const status = store.getStatus();
    const isActive = status?.session_state === "ACTIVE";
    if (isActive && !wasActive && !activeModal) {
      activeModal = showModal(<ActiveModal />, undefined, {
        strTitle: PLUGIN_NAME,
        bHideMainWindowForPopouts: false,
        // dismissed some other way: forget the handle so Close() is never called twice
        fnOnClose: () => {
          activeModal = null;
        },
      });
    } else if (!isActive && activeModal) {
      closeActiveModal();
    }
    wasActive = isActive;
  });

  const statusListener = addEventListener<[status: Status]>("status", onStatusEvent);
  const toastListener = addEventListener<[event: ToastEvent]>("toast", onToastEvent);

  // also re-shows the ACTIVE modal if the frontend reloaded mid-session
  void refreshStatus();

  return {
    name: PLUGIN_NAME,
    titleView: <div className={staticClasses.Title}>{PLUGIN_NAME}</div>,
    content: <Content />,
    icon: <FaGamepad />,
    onDismount() {
      removeEventListener("status", statusListener);
      removeEventListener("toast", toastListener);
      unsubscribe();
      closeActiveModal();
    },
  };
});
