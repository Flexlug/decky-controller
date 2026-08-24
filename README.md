**English** | [Русский](README.ru.md)

# Decky Controller

A plugin for [Decky Loader](https://decky.xyz). While it is enabled, the Steam Deck acts as a wired USB
gamepad for the computer attached to its USB‑C port. Two profiles are supported: XInput (Xbox 360
compatible) and a generic HID gamepad. In the active mode the Deck's built‑in controller is captured and
forwarded to the PC and the Deck's screen is turned off; the mode ends when a configured button combination
is held (L4+R4 by default), when the stop button in the UI is pressed, or when the cable is unplugged.
Nothing has to be installed on the PC.

The plugin only changes kernel state in memory (see "What the plugin changes on the system"); a reboot
returns the Deck to its original state.

## Requirements

* Steam Deck, SteamOS 3.6 or newer, Decky Loader installed.
* USB Dual‑Role Device (DRD) enabled in the BIOS — see below, a one‑time change.
* A USB‑C cable with data lines. Charge‑only cables do not work: the PC will not see a device.

### Enabling DRD in the BIOS

By default the USB‑C port is host‑only (docks, peripherals). With DRD the port can also act as a device when
it is connected to a computer; with a dock or a peripheral attached it still acts as a host — the role is
chosen automatically from what is plugged in.

1. Power the Deck off completely.
2. Hold Volume Up and press Power; release both after the chime — the BIOS menu opens.
3. Select **Setup Utility**.
4. Open **Advanced → USB Configuration → USB Dual‑Role Device**.
5. Change the value from **XHCI** to **DRD**.
6. Save and exit (**Exit → Exit Saving Changes** or F10). The Deck reboots.

The **How to enable DRD** button in the plugin shows the same steps.

## Installation

Three ways; the plugin is the same in all of them and updates work in each.

* **From the Decky Plugins Extended store** — a community store that merges the official catalog with
  plugins that are not in it. In Decky open **Settings → General → Store Channel**, set it to `Custom`,
  and set **Custom Store** to `https://decky-extended-plugins.beallio.com/plugins.json`. Then install
  **Decky Controller** from the Decky store as usual; updates appear there too.
* **By URL** — enable **Decky → Settings → Developer mode**, then **Decky → Developer → Install Plugin
  from URL**, paste
  `https://github.com/flexlug/decky-controller/releases/latest/download/decky-controller.zip`.
* **From a file** — download `decky-controller.zip` from the same link, copy it to the Deck (SD card, USB
  stick, `scp`) and choose **Decky → Developer → Install Plugin from ZIP** (Developer mode on).

The plugin appears in the Quick Access Menu on the Decky tab.

### Why the plugin is not in the official Decky store

The submission rules of the official plugin database require that the majority of the code was not written
by generative AI. This plugin's code was written with AI coding agents (reviewed and tested by a human on a
real device), so it is not submitted there; it is available from the
[Decky Plugins Extended](https://github.com/beallio/decky-plugins-extended) store instead. The official
store's other technical requirements are met.

## Usage

1. Connect the Deck to the PC with a USB‑C cable. A dock must be disconnected: the port works either as a
   host or as a device.
2. Open the Quick Access Menu (… button) → **Decky Controller**. The **Status** section shows: **DRD** —
   whether the BIOS mode is enabled; **Cable** — what the port currently sees (**PC**, **Charger**,
   **Dock**, **Not connected**, **Unknown**); **Controller** — the state of the built‑in controller;
   **Mode** — the session state. Once the mode is on, the **Cable** row is replaced by **Host**:
   **Waiting…** until the PC has recognised the device, then **Connected**.
3. Turn the **Controller mode** switch on. The built‑in controller is captured, the screen turns off, and
   the PC detects a new controller within a few seconds. `joy.cpl` (Windows) or any game can be used to
   check it.
4. To exit: hold the exit combination for 1.5 s (L4+R4 by default); or touch the screen (it turns on for a
   few seconds) and press **Stop controller mode** in the overlay; or unplug the cable. The screen turns on and the controller
   returns to Steam.

In the active mode the Deck does not react to its own buttons, Steam and QAM included: all input goes to the
PC.

### Profiles

| Profile | What the PC sees | Intended for |
|---|---|---|
| **XInput** (Xbox 360) — default | a wired XInput controller (VID 045E / PID 028E); Windows uses its standard `xusb22.sys` driver, Linux uses `xpad` | Windows games, emulators, any software with XInput support |
| **Generic HID** | a USB HID gamepad: 6 axes, hat, 16 buttons | Linux, DirectInput‑only software, a fallback |

XInput mapping: A/B/X/Y → A/B/X/Y, L1/R1 → LB/RB, L2/R2 → triggers, sticks → sticks, L3/R3, View → Back,
Menu → Start, D‑pad → D‑pad. The Steam and QAM buttons are not forwarded. The back buttons L4/L5/R4/R5 are
unassigned by default; assignments are made in the **Back paddles** section. The exit combination is not
forwarded to the PC.

### Settings

* **Profile** — XInput (Xbox 360) or Generic HID.
* **Kill switch** — the exit combination: L4+R4, L5+R5, L4+L5+R4+R5 or Steam+QAM; held for 1.5 s. The hold
  time is not configurable in the UI; if needed, change `kill_hold_ms` in
  `~/homebrew/settings/decky-controller/settings.json`.
* **Turn screen off while active** — turn the screen off for the duration of the session. In Gaming Mode
  the panel is turned off through gamescope; in Desktop Mode KDE DPMS is used when available, otherwise the
  backlight is dimmed. Touching the screen turns it on for a few seconds.
* **Back paddles** — what L4/L5/R4/R5 send to the PC: A/B/X/Y, LB/RB, L3/R3, View/Menu, D‑pad directions,
  or nothing.

## Limitations

* In the active mode the USB‑C port is taken by the link to the PC: docks, hubs, Ethernet adapters and other
  USB devices cannot be used (plugging one in switches the port back to host mode). Charging comes from the
  PC's USB port within that port's limits.
* The Deck's UI is unavailable in the active mode, the Steam button included.
* Gyro, trackpads, rumble and Bluetooth are not supported in the current version.
* If Windows is installed on a second partition of the Deck: with DRD enabled it does not see the USB port
  (Windows has no driver for the dual‑role controller); for USB under Windows set the BIOS value back to
  XHCI. SteamOS works in both modes.

## Troubleshooting

* **DRD: Disabled** — follow "Enabling DRD in the BIOS". After enabling, the row shows **Enabled** as soon as
  SteamOS has booted.
* **Cable: Not connected** with a cable attached — the port sees no power: the cable or the port is faulty,
  or the PC's port is asleep. Try another cable with data lines or another port; some PCs need a second after
  plugging in. **Charger** — a charger is connected, not a PC. **Dock** — a dock or a peripheral is
  connected, disconnect it. **Unknown** — readings are not available yet; reopen the panel.
* **The mode does not start or the PC sees no device** — press **Stop (full reset)**, reconnect the cable,
  try again.
* **The built‑in controller does not work after a failure** — press **Stop (full reset)** (the touchscreen
  always works) or reboot the Deck: the changes live only in kernel memory.
* **Logs** — **Decky → Settings → Developer → Show plugin logs**, or the files in
  `~/homebrew/logs/decky-controller/`. The **Diagnostics** button in the panel prints the state and the last
  50 lines of the daemon log.

## What the plugin changes on the system

For the duration of a session the plugin: unbinds the built‑in controller from the `usbhid` driver; creates
a USB gadget on the USB‑C port through the kernel's `raw_gadget` or `configfs` interfaces; turns the display
off (gamescope, KDE DPMS or the backlight). All of this is kernel state in RAM. The root filesystem
(read‑only), the bootloader, the EFI partition and the BIOS are not modified. The only persistent setting is
the DRD switch in the BIOS, which the user changes themselves. The changes are rolled back when the session
ends, when the plugin is stopped or unloaded; as a last resort a reboot removes them.

## Development

Build, checks, packaging, installing on the Deck, running the daemon by hand — [docs/DEV.md](docs/DEV.md).
Architecture — [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md); verified hardware facts —
[docs/HARDWARE.md](docs/HARDWARE.md).

## References

[GadgetDeck](https://github.com/Frederic98/GadgetDeck), [DeckJoy](https://github.com/Lucaber/deckjoy),
[DeckMTP](https://github.com/dafta/DeckMTP), [360‑raw‑gadget](https://github.com/CasperVM/360-raw-gadget),
the Linux [raw‑gadget](https://docs.kernel.org/usb/raw-gadget.html) interface,
[SDL](https://github.com/libsdl-org/SDL) (Steam Deck HID report layout and commands), the Decky Loader
plugin platform.

## License

MIT — see [LICENSE](LICENSE). Portions are derived from
[decky-plugin-template](https://github.com/SteamDeckHomebrew/decky-plugin-template) (BSD‑3‑Clause) — see
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
