# Decky Controller — verified hardware facts

What was checked on a real Steam Deck while designing the plugin, and what the code relies on. Reference
device: Steam Deck OLED 1 TB ("Galileo"), BIOS F7G0114, SteamOS 3.8.16, kernel 6.16.12‑valve24.5, Python 3.13.
Also verified end to end (controller mode on a PC, exit by combo, rollback) on a Steam Deck LCD 256 GB ("Jupiter")
with the same SteamOS release; the facts below hold on both models unless a line says otherwise.

## USB‑C port: DRD and role switching

* The USB‑C port sits on the AMD VanGogh USB2 controller, PCI `04:00.3` (a Synopsys DWC3 inside the APU).
  The built‑in controller lives on a *different* xHCI, `04:00.4`, so putting the port into device mode never
  touches it; Bluetooth is on UART.
* **BIOS setting**: *Advanced → USB Configuration → USB Dual‑Role Device* = **DRD** (default XHCI). With XHCI
  the function is `1022:162c` driven by `xhci_hcd` and `/sys/class/udc` does not exist. With DRD it becomes
  `1022:163a`; the kernel overrides the PCI class so that `dwc3-pci` claims it
  (`pci 0000:04:00.3: PCI class overridden (0x0c0330 -> 0x0c03fe) so dwc3 driver can claim this instead of xhci`)
  → platform device `dwc3.1.auto` (`dr_mode=otg`). `modprobe -R <modalias>` resolving to `dwc3_pci` is how the
  plugin detects DRD.
* **Role is decided by the EC**, not by us: the Valve MFD driver (`steamdeck.ko`, ACPI `VLV0100`, method `PDCS`:
  bit0 connect, bit3 data role) registers `extcon0 = steamdeck-extcon`; Valve's dwc3 patches take the role from
  that extcon (`linux,extcon-name`). `USB-HOST=1` (dock/peripheral attached) → host mode, child `xhci-hcd.2.auto`,
  the dock works; otherwise → device mode, child `gadget.0`, and `/sys/class/udc/dwc3.1.auto` appears
  (`state=not attached` until a gadget is bound; `maximum_speed=super-speed-plus`). `/sys/class/usb_role` is
  empty (no usb_role_switch). The PCI unbind/bind dance from 2023‑era references is unnecessary on this kernel.
* Connection to the PC is observed via `/sys/class/udc/<udc>/state`
  (`not attached → attached → powered → default → addressed → configured`).
* **Idle cable detection** (what the port sees before a gadget exists): `/sys/class/power_supply/ACAD/online`
  (power present) + the negotiated USB‑PD contract from hwmon `steamdeck_hwmon` (channels labelled
  "PD Contract Voltage" / "PD Contract Current"): a PC/hub port negotiates 5 V, a PD charger 15–20 V.
* Windows installed on the Deck has no USB while DRD is on (no dual‑role driver) — SteamOS is unaffected.

## Kernel gadget stack

All present as modules on the Valve kernel, nothing to build: `raw_gadget`, `gadgetfs`, `usb_f_hid`,
`usb_f_fs`, `libcomposite`, `dwc3`, `dwc3_pci` (`CONFIG_USB_DWC3_DUAL_ROLE=y`, `CONFIG_USB_RAW_GADGET=m`,
`CONFIG_USB_CONFIGFS_F_HID=y`, `CONFIG_USB_F_FS=m`). `/proc/config.gz` is readable.

* **configfs + `f_hid`** (generic HID gamepad): `/sys/kernel/config/usb_gadget/<name>` with one `hid.usb0`
  function, bound to the UDC → `/dev/hidg0`. Verified: the PC (Linux) enumerates a 9‑byte‑report gamepad,
  `js1`/`event*`/`hidraw*` appear, ~8 ms median event spacing (limited by the Python loop, not USB).
* **raw‑gadget** (XInput): `/dev/raw-gadget` lets userspace answer EP0 and present arbitrary descriptors — in
  particular the vendor‑specific 0x21 descriptor of the Xbox 360 pad that FunctionFS rejects (it requires 9
  bytes for type 0x21). Verified: a Linux host enumerates `045e:028e`, `xpad` binds; Windows 11 (26100)
  enumerates "Xbox 360 Controller for Windows" with the built‑in `xusb22` driver, XInput slot 0 reports live
  input, the host sends LED (`01 03 0n`) and rumble (`02 08 03 …`) OUT reports, and empty replies to the
  capability requests (`0xc1/0x01`, `0xc0/0x01`) are accepted — no MS OS descriptor needed. The gadget survives
  a host reboot (DISCONNECT → CONNECT + SET_CONFIGURATION).
* raw‑gadget pitfalls learned the hard way: `fcntl.ioctl` holds the GIL, so a blocking `EVENT_FETCH` starves
  the IN‑writer thread → use `ioctl` through ctypes (GIL released). `RAW_GADGET_EP_DISABLE` (and `VBUS_DRAW`)
  take the value **by value** in the ioctl argument, not a pointer — passing a pointer yields `EBUSY` forever
  and endpoints are never released.

## Built‑in controller ("Neptune", USB 28de:1205)

Five interfaces: `1.0` HID boot mouse and `1.1` HID boot keyboard (lizard mode), `1.2` HID controller
(`hid-steam`, 64‑byte state reports), `1.3/1.4` CDC ACM (left alone). All three HID interfaces are bound to
`usbhid` (`/sys/bus/usb/drivers/usbhid/{bind,unbind}` exist). `hid-steam` creates a layered "client" hidraw
that Steam keeps open, and removes the evdev gamepad while a client holds it — there is no evdev node to read
while Steam runs; the virtual "Microsoft X‑Box 360 pad" (`28de:11ff`) is Steam Input's uinput device.

Exclusive capture therefore = unbind `1.0/1.1/1.2` from `usbhid` (Steam loses the controller, the Deck UI stops
reacting), claim interface 2 via usbfs (`/dev/bus/usb/BBB/DDD`, `USBDEVFS_CLAIMINTERFACE` / `BULK` / `CONTROL`
through ctypes ioctl), send lizard‑off (`ID_CLEAR_DIGITAL_MAPPINGS`, `ID_SET_SETTINGS_VALUES`) and a heartbeat,
parse the 64‑byte report. Rebinding to `usbhid` gives the controller back to Steam. Verified end‑to‑end: the
plugin's `run` captured the controller and a game on the PC was played from the Deck.

## Screen off

* Gaming Mode: gamescope's display sleep ConVar `drm_sleep_internal_screen` via `gamescopectl`
  (socket `/run/user/1000/gamescope-0`, env `XDG_RUNTIME_DIR=/run/user/1000 GAMESCOPE_WAYLAND_DISPLAY=gamescope-0`)
  — the same mechanism Steam uses for its idle "turn off screen"; the panel (CRTC) is really off.
* Desktop Mode: `kscreen-doctor --dpms off` (KDE) when available.
* Fallback: `/sys/class/backlight/amdgpu_bl0/brightness = 0` only **dims** the OLED to its minimum; it does not
  turn it off. `/sys/class/drm/card0-eDP-1/dpms` is read‑only; `status` is writable (force off) but breaks the
  compositor — not used. `bl_power` exists but is not honoured for the OLED.
* Touch wake: the touchscreen (`FTS3528:00 2808:1015` on the OLED, evdev) keeps working while the controller is
  captured. The daemon finds it by capabilities (`INPUT_PROP_DIRECT` + `ABS_MT_POSITION_X`, the udev
  `ID_INPUT_TOUCHSCREEN` test), so the LCD model's panel controller is picked up the same way (confirmed).

## Sources

* SDL (zlib): `src/joystick/hidapi/SDL_hidapi_steamdeck.c`, `steam/controller_structs.h`,
  `steam/controller_constants.h` — Deck report layout, lizard‑off and heartbeat commands.
* Linux kernel: `drivers/usb/gadget/legacy/raw_gadget.c` and `Documentation/usb/raw-gadget.rst`;
  `drivers/usb/gadget/function/f_hid.c` / `f_fs.c` (descriptor validation); `drivers/hid/hid-steam.c`
  (facts only — GPL, no code copied); `include/uapi/linux/usb/raw_gadget.h`, `usbdevice_fs.h`.
* Valve kernel branch (`linux-neptune`, `6.5/features/usb` and successors): "Hardcode jupiter ACPI device as
  extcon name", "Drop usb-role-switch", "Bump USB gadget wakeup timeout"; `drivers/mfd/steamdeck.c`
  (EC/extcon); the in‑tree `steamdeck-extcon` driver.
* Xbox 360 wired protocol: GP2040‑CE `XInputDescriptors.h` (facts only), Parts Not Included "Understanding
  the Xbox 360 wired controller's USB data", 360‑raw‑gadget.
* Windows acceptance was verified on Windows 11 Pro 26100 with a PowerShell/XInput test script.
