# Decky Controller — architecture

For contributors and coding agents. This is the condensed contract between the three parts of the plugin
(frontend ↔ backend ↔ daemon); hardware facts are in [HARDWARE.md](HARDWARE.md), workflows in [DEV.md](DEV.md).

## Overview

```
Quick Access Menu (src/*)  --callables/events-->  main.py + py_modules/controller_backend (Decky backend, root)
                                                     |  subprocess: python3 -m deckgadget run …  (JSON-lines on stdout)
                                                     v
                                   py_modules/deckgadget (daemon: capture Neptune -> profile.pack -> USB gadget)
```

* The **frontend** is a React panel in the Quick Access Menu; it only talks to the backend.
* The **backend** (`main.py` glue + `py_modules/controller_backend`, runs as root inside Decky Loader) owns settings,
  starts/stops the daemon, relays its
  events as `status`, and guarantees rollback (`deckgadget recover`) on load, after every daemon exit and on unload.
* The **daemon** (`py_modules/deckgadget`) is a normal Python package and CLI: it captures the built‑in controller
  through usbfs, packs reports into the selected profile and pushes them to the PC through a USB gadget.

Requirements (minimums): any Steam Deck; SteamOS 3.6+ (Valve kernel with EC‑extcon USB role switching and the
`raw_gadget` / `f_hid` modules — verified on SteamOS 3.8.16 / OLED; LCD testing planned); Decky Loader;
Python 3.11+.

## Module map

Frontend (`src/`, TypeScript/React, bundled by rollup into `dist/index.js`):

| file | role |
|---|---|
| `index.tsx` | plugin entry (`definePlugin`): registers the panel, subscribes to `status`/`toast` for the whole session, shows/hides the ACTIVE modal |
| `Content.tsx` | the QAM panel: Controller‑mode toggle, Status rows (DRD / Cable or Host / Controller / Mode), Settings, Back paddles, Tools |
| `modals.tsx` | ACTIVE overlay ("hold the exit combo…" + Stop), DRD/BIOS instructions, raw diagnostics |
| `actions.ts` | high‑level operations: call the backend, push results into the store, surface errors as toasts |
| `api.ts` | typed wrappers around the backend callables (names/args are part of the contract) |
| `store.ts` | tiny module‑level store for the latest Status/Settings (survives panel unmounts) |
| `types.ts` | Status/Settings/enum types, defaults, UI labels (≤ 14 chars for status values and dropdown options) |

Backend — `main.py` is Decky glue only (imports `decky`, routes the package loggers to Decky's log, builds the
`Service`, `class Plugin` with the callables below); the logic lives in `py_modules/controller_backend/`, which never
imports `decky` or `deckgadget` (it gets logger/emit/directories injected and runs the daemon as a subprocess). It
may import `deckhw` (read-only sysfs facts, shared with the daemon):

| module | role |
|---|---|
| `settings.py` | allowed values, `DEFAULT_SETTINGS`, `sanitize_settings`, `SettingsStore` (`settings.json`, atomic writes) |
| `daemon/launcher.py` | interpreter/module/cwd, `run_args` from the settings, environment (`LD_LIBRARY_PATH` dropped), pidfile/log paths |
| `daemon/supervisor.py` | `DaemonSupervisor`: spawn, stdout/stderr pumps, SIGTERM → SIGKILL, pidfile, stale-daemon kill, exit callback |
| `daemon/events.py` | stdout JSON-lines contract: event names, session states, parsing |
| `daemon/commands.py` | one-shot `status` / `recover` runs with timeouts, CLI status normalisation, `RecoverReport` |
| `session.py` | `SessionView`: the session as seen from the events (`STOPPED` → `STOPPING` until the process is gone) |
| `status.py` | `hardware_facts` (via `deckhw`), `build_status` (CLI first, sysfs fallback, session view), connectivity signature |
| `diagnostics.py` | the Diagnostics dump (status, settings, daemon info, last recover, log tails) |
| `service.py` | `Service`: start/stop under one lock, recover policy (load, every exit, stop), 2 s / 5 s status loop, emits |

Shared (`py_modules/deckhw/`): `sysfs.py` (tree reader), `drd.py`, `udc.py`, `extcon.py`, `cable.py`, `neptune.py`
(device discovery), `port.py` (`PortStatus`) — read-only, no ioctl, no writes.

Daemon (`py_modules/deckgadget/`):

| module | role |
|---|---|
| `__main__.py` | CLI: `run \| demo \| status \| recover \| probe`; `collect_status` assembles the `status` JSON |
| `config.py` | allowed values + validation of the run options (profiles, transports, kill combos, paddles, screen methods); resolves `transport=auto` |
| `state.py` | canonical `ControllerState` (own button numbering, sticks, triggers, pads, sensors) |
| `session.py` | the session state machine, kill‑combo hold detector, unplug detection, hot loop; `build_session` wires source/profile/transport/screen |
| `sources/base.py`, `sources/demo.py` | `InputSource` protocol; synthetic source (sine sticks, blinking A) for `demo` |
| `sources/neptune/protocol.py` | the Deck's 64‑byte input report: layout, button tables, `parse_report` / `decode_report` (SDL) |
| `sources/neptune/commands.py` | feature reports sent to the controller: lizard‑off, heartbeat, rumble (SDL) |
| `sources/neptune/source.py` | `NeptuneUsbSource`: exclusive capture over usbfs (claim interface 2, heartbeat thread, rebind on close) |
| `profiles/base.py` | `Profile` protocol: USB descriptors + `pack(state) -> bytes` + `on_output(bytes)` + EP0 `handle_control` |
| `profiles/xbox360.py` | XInput profile: wired Xbox 360 descriptors (VID 045E / PID 028E, vendor 0x21), 20‑byte report, LED/rumble OUT |
| `profiles/hid_gamepad.py` | generic HID gamepad (6 × int8 axes, hat, 16 buttons) |
| `transports/base.py` | `Transport` protocol, latest‑report slot (newest wins, drops counted), metrics, thread interrupt helpers |
| `transports/rawgadget/transport.py` | `/dev/raw-gadget` transport: lifecycle, event loop, IN/OUT worker threads, endpoint teardown |
| `transports/rawgadget/control.py` | EP0 handling: standard device requests, interface/endpoint recipients, delegation to the profile |
| `transports/usb_hid.py` | configfs + `f_hid` transport (`/sys/kernel/config/usb_gadget/deckctl_hid`, `/dev/hidgN`) |
| `platform/usbfs.py` | usbfs client (ctypes structs, `USBDEVFS_*` ioctls, `UsbfsDevice`) — nothing Deck‑specific |
| `platform/rawgadget/ioctls.py`, `platform/rawgadget/device.py` | raw‑gadget ioctl ABI (`raw_gadget.h`) and `RawGadgetDevice` |
| `platform/neptune_binding.py` | the only place that writes `usbhid` bind/unbind: `UsbhidBinder`, capture/release interfaces |
| `platform/display/base.py` | `ScreenMethod` protocol, backlight state‑file location |
| `platform/display/backlight.py` | `Backlight` (save/dim/restore; never restores to 0), `BacklightDim` strategy |
| `platform/display/compositor.py` | `run_command`, `GamescopeSleep` (`gamescopectl drm_sleep_internal_screen`), `KscreenDpms` |
| `platform/display/touch.py` | touchscreen discovery and the evdev `TouchWatcher` (touch‑to‑wake) |
| `platform/display/controller.py` | `ScreenController`: picks the first strategy whose sleep works, wake/re‑sleep on touch |
| `platform/guard.py` | `recover()`: idempotent "undo everything" in four steps (gadgets, rebind, wake, backlight) |
| `util/fs.py`, `util/ioctl.py`, `util/log.py` | file helpers; ctypes `ioctl` (releases the GIL) + `_IOC` macros; stderr/file logging + JSON‑lines event sink |

## Backend callables

All `async`, all return JSON‑compatible dicts; errors are returned as `{"ok": false, "error": "..."}` rather
than raised, so the frontend always gets a dict.

| callable | args | returns |
|---|---|---|
| `get_status` | — | `Status` |
| `start` | `profile: "xbox360" \| "hid_gamepad"` (optional, defaults to the setting) | `Status` after the start attempt |
| `stop` | — | `Status`; idempotent full rollback, always safe to call |
| `get_settings` | — | `Settings` |
| `set_settings` | `settings: dict` (partial; `paddles` may be partial) | merged, persisted `Settings` |
| `get_diagnostics` | — | dict: raw `deckgadget status`, versions, settings, daemon info, last recover report, last 50 log lines |

Events (`decky.emit`): `status` (a `Status`, on every daemon state/connection change and at least every 2 s
while the daemon lives) and `toast` (`{"title", "body", "severity": "info|warn|error"}`).

### Status

```json
{"ok": true, "plugin_version": "0.1.0", "kernel": "6.16…", "model": "Galileo",
 "drd_enabled": true, "udc_name": "dwc3.1.auto", "udc_state": "configured|not attached|…|null", "udc_speed": "…|null",
 "extcon": {"USB": 0, "USB-HOST": 0}, "host_connected": true,
 "cable_power": true, "pd_contract_mv": 5000, "pd_contract_ma": 1500, "cable_kind": "pc",
 "neptune_present": true, "neptune_captured": false,
 "daemon_running": false, "daemon_pid": null,
 "session_state": "IDLE|CAPTURING|GADGET_UP|WAITING_HOST|ACTIVE|STOPPING", "session_detail": "",
 "active_profile": "xbox360|hid_gamepad|null", "transport": "raw|hid|null",
 "screen_off": false, "last_error": null, "metrics": {"hz": 0, "reports": 0, "dropped": 0},
 "status_error": null}
```

* `host_connected` = (`udc_state == "configured"`): true only while our gadget is bound and the PC has enumerated
  it — always `false` in IDLE, even with a PC on the cable.
* `cable_*` describe what the port *physically* sees, independent of the gadget (optional keys, may be `null`):
  `cable_power` = `/sys/class/power_supply/ACAD/online`; `pd_contract_mv/ma` = the negotiated USB‑PD contract from the
  `steamdeck_hwmon` hwmon (labels "PD Contract Voltage/Current"); `cable_kind` ∈ `none | pc | charger |
  host_device | unknown` — `USB-HOST=1` → `host_device` (dock/peripheral, port is a host); else no power → `none`;
  else contract ≤ 5.5 V → `pc`, > 5.5 V → `charger`; else `unknown`.
  The UI shows only the classification (PC / Charger / Dock / Not connected / Unknown); volts, amps or the words
  "PD contract" never appear in the UI — the backend fields exist for classification and diagnostics only.
* `status_error` non‑null ⇒ the hardware fields came from the backend's sysfs fallback instead of `deckgadget status`.

### Settings (defaults)

```json
{"profile": "xbox360", "transport": "auto", "kill_combo": "L4+R4", "kill_hold_ms": 1500,
 "screen_off": true, "touch_wake_seconds": 5,
 "paddles": {"L4": "none", "L5": "none", "R4": "none", "R5": "none"}}
```

Allowed values: `profile` ∈ {`xbox360`, `hid_gamepad`}; `transport` ∈ {`auto`, `raw`, `hid`} (`auto` = `raw` for
xbox360, `hid` for hid_gamepad; xbox360 over `hid` is rejected — `f_hid` cannot expose the vendor interface);
`kill_combo` ∈ {`L4+R4`, `L5+R5`, `L4+L5+R4+R5`, `STEAM+QAM`}; `kill_hold_ms` 100..10000 (the daemon's limit —
the backend clamps the setting to 200..10000); `touch_wake_seconds` (0, 120] (backend clamps to 1..60);
`paddles.*` ∈ {`none`, `A`, `B`, `X`, `Y`, `LB`, `RB`, `L3`, `R3`, `VIEW`, `MENU`, `DPAD_UP`,
`DPAD_DOWN`, `DPAD_LEFT`, `DPAD_RIGHT`}. The single source of truth for these lists is
`py_modules/deckgadget/config.py`; `py_modules/controller_backend/settings.py` and `src/types.ts` mirror them.

## Daemon CLI and events

```
python3 -m deckgadget run    --profile xbox360|hid_gamepad --transport auto|raw|hid \
                             --kill-combo "L4+R4" --kill-hold-ms 1500 [--screen-off] [--touch-wake-seconds 5] \
                             [--screen-method auto|gamescope|kscreen|backlight] [--paddles L4=none,L5=none,R4=none,R5=none] \
                             [--forward-steam] [--forward-qam] [--udc NAME] [--log-file PATH] [-v]
python3 -m deckgadget demo   … same flags; synthetic input source, no capture (transport test)
python3 -m deckgadget status [--no-modprobe]    # JSON snapshot: drd/udc/extcon/cable/neptune/gadgets/screen methods
python3 -m deckgadget recover [--log-file PATH] # idempotent full rollback, always exit 0, prints a JSON report
python3 -m deckgadget probe  [--seconds 10] [--all] [--json] [--sensors]   # capture Neptune, print decoded reports, then roll back
```

The backend starts `run` with `cwd=<plugin>/py_modules`, `LD_LIBRARY_PATH` removed from the environment (Decky's
environment breaks subprocesses otherwise), and passes profile/transport/kill combo/hold/screen‑off/touch‑wake/
paddles/log‑file from Settings (`--screen-method` is not passed: always `auto`).

`run`/`demo` write **JSON‑lines events to stdout** (one object per line; human logs go to stderr / `--log-file`):

```
{"ev":"state","state":"CAPTURING|GADGET_UP|WAITING_HOST|ACTIVE|STOPPING|STOPPED","detail":"…"}
{"ev":"error","msg":"…"}
{"ev":"metrics","hz":250,"reports":12345,"dropped":0}          # every 2 s
{"ev":"kill","reason":"combo|unplug|signal|error"}
{"ev":"screen","off":true,"method":"gamescope|kscreen|backlight|none"}
```

Every event also carries `"ts"`. The backend maps `state` → `Status.session_state` (`STOPPED` is shown as
`STOPPING` until the process is gone, then `IDLE`), `error` → `last_error`, `kill` → a toast, `screen` →
`Status.screen_off`. Signals: SIGTERM/SIGINT → graceful rollback, exit 0; any exception → rollback in `finally`,
exit ≠ 0 (the backend runs `recover` after every exit anyway).

## Session state machine

```
IDLE -> CAPTURING  screen off (gamescope sleep / kscreen DPMS / backlight); unbind usbhid from Neptune 1.0/1.1/1.2,
                   claim interface 2 via usbfs, lizard-mode off + heartbeat every ~1 s
     -> GADGET_UP  transport.start(profile): raw-gadget (xbox360) or configfs f_hid (hid_gamepad) bound to the UDC
     -> WAITING_HOST  until /sys/class/udc/<udc>/state == "configured"
     -> ACTIVE     hot loop: source.read -> profile.pack -> transport.send; OUT reports (LED/rumble) logged
     -> STOPPING -> STOPPED   rollback in reverse order, then exit
```

* The kill combo (held for `kill_hold_ms`) is armed from CAPTURING on and is never forwarded to the PC.
  Steam/QAM are not forwarded unless `--forward-steam` / `--forward-qam` (not exposed in the UI).
* Cable unplug in ACTIVE (UDC state leaves `configured` for longer than a short grace period) → `kill unplug`.
* Touching the touchscreen while the screen is off wakes it for `touch_wake_seconds`, then it sleeps again.
* Reports are delivered newest‑first: if the host polls slower than the source, older unsent reports are dropped
  (counted) so latency stays minimal and a stalled endpoint can never block kill‑combo detection.

## Safety and recovery rules

Every exit path — kill combo, unplug, signal, exception, backend `stop`, plugin unload/uninstall, backend start
after a crash — ends in the same idempotent rollback (`platform/guard.py:recover()`), which:

1. unbinds and deletes every configfs gadget under `/sys/kernel/config/usb_gadget/deckctl*` (raw‑gadget needs
   nothing: the gadget vanishes with the owning fd);
2. rebinds Neptune interfaces 1.0/1.1/1.2 to `usbhid` if they lost their driver, re‑scans and reports an error
   if any is still detached;
3. wakes the display (`gamescopectl drm_sleep_internal_screen 0` when a gamescope socket exists,
   `kscreen-doctor --dpms on` when a KDE Wayland session is reachable — failures are warnings);
4. restores the saved backlight value (`/run/deckgadget/brightness`).

The backend runs `recover` at plugin load (a previous instance may have died mid‑session), after **every** daemon
exit, and from `stop` (SIGTERM → wait ≤ 3 s → SIGKILL → `recover`). `start`/`stop`/`recover` never overlap
(one lock). A stale daemon from a previous backend instance is killed via the pidfile before recovering.
Nothing persistent is written outside Decky's settings/log directories; a reboot restores everything.

## Hard constraints

* **Python standard library + ctypes only** for the backend and daemon (SteamOS ships no pip; nothing is
  installed outside the plugin directory). Frontend dependencies are bundled by rollup at build time.
* **Never switch the USB port role yourself** (no PCI unbind/bind of `04:00.3`, no debugfs `mode` writes):
  the Valve EC driver → `steamdeck-extcon` → dwc3 does it automatically — host while a dock/peripheral is
  attached, device otherwise. The daemon only *observes* extcon and `/sys/class/udc/*`.
* **No code copied from GPL projects** (hid‑steam, InputPlumber, HHD, GP2040‑CE, …) — only facts and
  constants, with the source cited in a comment. SDL (zlib licence) is the protocol reference for the Deck's
  HID reports and commands (`SDL_hidapi_steamdeck.c`, `steam/controller_structs.h`,
  `steam/controller_constants.h`).
* Frontend text budget: status‑row values and dropdown option labels ≤ 14 characters; no volts/amps/"PD
  contract" in the UI.
* Names that are part of the contract (callables, event names, Status/Settings keys, CLI flags, enum values,
  module paths) are not renamed casually — update all three sides together (see the checklist in DEV.md).

## Why the plugin needs the `root` flag

`plugin.json` sets `"flags": ["root"]`, so the backend (and the daemon it spawns) run as root. Everything the
daemon touches is root‑only on SteamOS:

* `/sys/bus/usb/drivers/usbhid/{unbind,bind}` — detaching and reattaching the built‑in controller;
* `/dev/bus/usb/BBB/DDD` (usbfs) — claiming its interface and reading reports directly;
* `/dev/raw-gadget` — the raw USB gadget used for the XInput profile;
* `/sys/kernel/config/usb_gadget/…` (configfs) and `/dev/hidgN` — the `f_hid` gadget;
* `/sys/class/backlight/amdgpu_bl0/brightness` — the backlight fallback (and `gamescopectl` is invoked from
  that root context with the user's runtime dir).

Only `deckgadget status` works unprivileged (DRD/UDC/extcon are world‑readable).
