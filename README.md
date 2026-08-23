# Decky Controller

A [Decky Loader](https://decky.xyz) plugin that turns your Steam Deck into a USB game controller for another
PC — right from Gaming Mode. Plug the Deck into a PC with a USB‑C cable, flip **Controller mode** in the Quick
Access Menu, and the PC sees an ordinary USB gamepad — **XInput** (works in Windows out of the box) or **generic
HID**; more profiles are planned. The Deck's screen turns off while it is a controller; to get your Deck back,
hold the exit combo (**L4+R4** by default, configurable). No Desktop Mode, no password, nothing to install on
the PC.

Everything happens in memory: no system files are modified and a reboot always restores the Deck to normal.

## Requirements

* Steam Deck with SteamOS 3.6 or newer and Decky Loader.
* USB Dual‑Role Device (DRD) enabled in the BIOS (one‑time, steps below).
* A USB‑C **data** cable to the PC (many charge‑only cables have no data lines — if the PC never sees the Deck,
  try another cable).

### Enable DRD in the BIOS (one‑time)

By default the Deck's USB‑C port is host‑only (for docks and peripherals). Dual‑Role lets the port also act as a
device when it is plugged into a computer. While DRD is on, the port still works as a host whenever a dock or
peripheral is attached — the role follows what is plugged in.

1. Power the Deck **off** completely.
2. Hold **Volume Up** and press **Power**; release both when you hear the chime — the BIOS menu appears.
3. Choose **Setup Utility**.
4. Go to **Advanced → USB Configuration → USB Dual‑Role Device**.
5. Change it from **XHCI** to **DRD**.
6. Save and exit (**Exit → Exit Saving Changes**, or F10). The Deck reboots.

The plugin's **How to enable DRD** button shows the same steps on the Deck. Note: while DRD is on,
**Windows installed on the Deck** loses its USB port (Windows has no driver for the dual‑role controller).
Switch back to XHCI if you need USB under Windows‑on‑Deck; SteamOS is unaffected either way.

## Install

1. Download `decky-controller.zip` from GitHub Releases:
   <https://github.com/flexlug/decky-controller/releases/latest/download/decky-controller.zip>
2. On the Deck: **Decky → Settings → Developer mode** → on.
3. **Decky → Developer → Install Plugin from URL** and paste the URL above — or **Install Plugin from ZIP** if
   you copied the zip to the Deck (SD card, USB stick, …).
4. **Decky Controller** appears in the Quick Access Menu (the Decky tab).

The release zip is currently the only distribution channel — the plugin is not listed in the official Decky
store or in any third‑party store.

### Why isn't this in the official Decky store?

The official plugin database's submission checklist requires that generative AI was not used to write the
majority of the code. This plugin was written with AI coding agents (reviewed and tested by a human on real
hardware), so it will not be submitted there. It is distributed via GitHub Releases / URL install instead,
while still following the store's other technical requirements.

## Usage

1. Plug the Deck into the PC with a USB‑C data cable (remove the dock — the port can only do one job at a time).
2. Open the Quick Access Menu (… button) → **Decky Controller**. The **Status** section shows **DRD**,
   **Cable** (what the port sees while idle: **PC**, **Charger**, **Dock**, **Not connected**, **Unknown**),
   **Controller** and **Mode**. Once controller mode is on, the cable row becomes **Host** and shows
   **Waiting…** until the PC has enumerated the controller, then **Connected**.
3. Toggle **Controller mode** on. The Deck's built‑in controller is taken over, the screen turns off, and the PC
   sees a new controller within a couple of seconds. Use `joy.cpl` on Windows or any game to test.
4. Play. To stop: hold the exit combo (**L4+R4** by default, 1.5 s), or tap the screen (it wakes for a few
   seconds) and press **Stop (full reset)**, or unplug the cable. The screen comes back and Steam sees the
   controller again.

While Controller mode is active the Deck itself does not react to any input — including the Steam and QAM
buttons — because every report goes to the PC. That is by design.

### Profiles

| Profile | PC sees | Best for |
|---|---|---|
| **XInput** (Xbox 360 compatible) — default | a wired XInput controller (VID 045E / PID 028E), handled by Windows' built‑in `xusb22.sys`; Linux `xpad` | Windows games inside and outside Steam, emulators, anything XInput |
| **Generic HID** | a standard USB HID gamepad (6 axes, hat, 16 buttons) | Linux hosts, DirectInput‑only software, as a fallback |

Default mapping (XInput): A/B/X/Y → A/B/X/Y, L1/R1 → LB/RB, L2/R2 → triggers, sticks → sticks, L3/R3,
View → Back, Menu → Start, D‑pad → D‑pad. Steam and QAM are **not** forwarded. The four back paddles
(L4/L5/R4/R5) can be mapped to any button in the **Back paddles** section; the exit combo is never sent to the PC.

### Settings

* **Profile** — XInput (Xbox 360) / Generic HID.
* **Kill switch** (the exit combo) — L4+R4, L5+R5, L4+L5+R4+R5 or Steam+QAM, held for 1.5 s. The hold time
  is not in the panel; change `kill_hold_ms` in `~/homebrew/settings/decky-controller/settings.json` if needed.
* **Turn screen off while active** — saves battery and hides the frozen UI; touching the screen wakes it for a
  few seconds so you can reach **Stop**. In Gaming Mode the panel is really turned off (gamescope display
  sleep); Desktop Mode uses KDE DPMS if available, otherwise only dims the backlight.
* **Back paddles** — what L4/L5/R4/R5 send to the PC (A/B/X/Y, LB/RB, L3/R3, View/Menu, D‑pad or nothing).

## Limitations

* The USB‑C port is *either* a host port *or* the controller link. Docks, hubs, Ethernet adapters and other
  USB devices cannot be used while Controller mode is on (plugging one in makes the port a host again).
  Charging through the PC's USB port is limited to what that port supplies.
* The Deck's UI is fully locked out while active (Steam button included) — by design.
* Gyro, trackpads, rumble and Bluetooth output are not in this version.
* Windows installed **on the Deck** has no USB while DRD is enabled (see above).

## Troubleshooting

* **DRD: not detected / "DRD not enabled"** → follow the BIOS steps above; after enabling, the panel shows DRD on
  as soon as SteamOS boots.
* **Cable: Not connected** although the cable is in → the port sees no power at all: the cable or port is dead,
  or the PC port is asleep. Try another cable (it must carry data) or port; some PCs need a second after
  plugging in. **Charger** → that is a charger, not a PC. **Dock** → unplug the dock/accessory. **Unknown** →
  readings not available yet; re‑open the panel in a second.
* **Controller mode won't start / the PC sees nothing** → press **Stop (full reset)**, re‑plug, try again.
* **The Deck's own controller is dead after something went wrong** → press **Stop (full reset)** (the
  touchscreen always works), or simply reboot: all changes live in kernel memory and vanish on reboot.
* **Logs**: **Decky → Settings → Developer → Show plugin logs**, or the files under
  `~/homebrew/logs/decky-controller/` (`~/homebrew` is Decky Loader's home directory on the Deck). The panel's
  **Diagnostics** button includes the last 50 daemon lines.

## Safety

The plugin only changes volatile kernel state: it unbinds the built‑in controller from the `usbhid` driver
while active, creates a USB gadget on the USB‑C port through the kernel's `raw_gadget` / `configfs` interfaces,
and puts the display to sleep (gamescope / KDE DPMS, or dims the backlight). Nothing is written to the
read‑only root filesystem, the bootloader, the EFI partition or the BIOS. The only persistent setting is the
DRD toggle in the BIOS, which you change yourself. Every step is undone when the session ends, when the plugin
stops or unloads, and — in the worst case — by a reboot.

## Development

See [docs/DEV.md](docs/DEV.md) (build, checks, packaging, install on the Deck, running the daemon by hand) and
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) / [docs/HARDWARE.md](docs/HARDWARE.md).

## Credits

Prior art and references this project stands on: [GadgetDeck](https://github.com/Frederic98/GadgetDeck),
[DeckJoy](https://github.com/Lucaber/deckjoy), [DeckMTP](https://github.com/dafta/DeckMTP),
[360‑raw‑gadget](https://github.com/CasperVM/360-raw-gadget), the Linux
[raw‑gadget](https://docs.kernel.org/usb/raw-gadget.html) interface, and
[SDL](https://github.com/libsdl-org/SDL) (Steam Deck HID report layout and commands). Thanks to the Decky
Loader team for the plugin platform.

## License

MIT — see [LICENSE](LICENSE). Portions are derived from
[decky-plugin-template](https://github.com/SteamDeckHomebrew/decky-plugin-template) (BSD‑3‑Clause) — see
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
