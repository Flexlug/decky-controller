# Decky Controller — developer guide

Short, practical notes on building, checking, packaging, installing and debugging the plugin.
Architecture and contracts: [ARCHITECTURE.md](ARCHITECTURE.md); verified hardware facts: [HARDWARE.md](HARDWARE.md).

## Layout

```
src/              frontend (TypeScript/React, @decky/ui + @decky/api)  -> dist/index.js (rollup)
main.py           Decky glue: imports decky, wires the backend service, class Plugin (callables)
py_modules/
  controller_backend/   the backend: settings, daemon/ (launcher, supervisor, events, commands), session,
                  status, diagnostics, service
  deckhw/         read-only hardware facts from sysfs (shared by backend and daemon)
  deckgadget/     daemon + core: python3 -m deckgadget run|demo|status|recover|probe
tests/            stdlib unittest suite for the core (no hardware needed)
scripts/build-zip.sh   packages out/decky-controller.zip
.github/workflows/build.yml   CI (see below)
```

Constraints:

* Backend and daemon: Python standard library + ctypes only (SteamOS ships no pip). Frontend is bundled by rollup.
* Target: SteamOS 3.6+ (Python 3.11+; the Deck in use runs SteamOS 3.8 / Python 3.13 / kernel 6.16‑valve).

## Build

The frontend toolchain uses **pnpm** 9 (`package.json` → `packageManager`). Either enable corepack once
(`corepack enable`) or run it ad hoc with `corepack pnpm@9 …` / `npx pnpm@9 …`.

```sh
pnpm install --frozen-lockfile
pnpm run build        # rollup -> dist/index.js
pnpm run watch        # rebuild on change
```

## Checks

```sh
pnpm run typecheck    # tsc --noEmit
pnpm test             # = python3 -m compileall -q py_modules main.py && python3 -m unittest discover -s tests -v
```

Tests run on any Linux host (Python ≥ 3.11); everything that touches sysfs/configfs/usbfs takes injectable
paths and is exercised against fake trees in a temp dir. The backend is importable without Decky
(`cd py_modules && python3 -c "import controller_backend.service"`; only `main.py` needs the `decky` module —
tests stub it via `tests/decky_stub.py`), and `(cd py_modules && python3 -m deckgadget status)` runs on any
Linux box.

## Packaging

`scripts/build-zip.sh` → `out/decky-controller.zip` (builds the frontend first unless `--no-build`).

## CI

`.github/workflows/build.yml` runs the build and checks on every push / pull request and, on tags `v*`,
publishes a GitHub Release with `decky-controller.zip` attached. The latest release is always at
`https://github.com/flexlug/decky-controller/releases/latest/download/decky-controller.zip`.

## Install on the Steam Deck

Prerequisites on the Deck: Decky Loader and DRD enabled in the BIOS (see the README). The plugin needs
`"flags": ["root"]` — already set in `plugin.json`.

Get the zip onto the Deck any way you like — SD card, USB stick, `scp`, or simply use the release URL — then:
**Decky → Settings → Developer mode** → on, then **Decky → Developer → Install Plugin from ZIP** (pick the file)
or **Install Plugin from URL** (`https://github.com/flexlug/decky-controller/releases/latest/download/decky-controller.zip`).

## Running the daemon standalone (on the Deck, as root)

The core is a normal Python package; run it from the plugin's `py_modules` directory. All commands are
read‑only except `run`/`demo`/`probe` (which capture the controller and/or create a USB gadget) and `recover`
(which undoes exactly that).

```sh
cd ~/homebrew/plugins/decky-controller/py_modules      # or the repo's py_modules/ when developing
sudo python3 -m deckgadget status                      # JSON: DRD, UDC name/state, extcon cables, cable kind, Neptune, gadgets, screen methods
sudo python3 -m deckgadget probe --seconds 10          # capture the built-in controller, print decoded reports (bit calibration)
sudo python3 -m deckgadget probe --json --sensors      # machine-readable, with gyro/accel/pads
sudo python3 -m deckgadget demo --profile xbox360      # bring the gadget up with a synthetic source (no capture) — transport test
sudo python3 -m deckgadget run --profile xbox360 --transport auto --kill-combo "L4+R4" --kill-hold-ms 1500 \
        --screen-off --touch-wake-seconds 5 --paddles L4=none,L5=none,R4=none,R5=none --log-file /tmp/deckgadget.log
sudo python3 -m deckgadget recover                     # idempotent full rollback (gadget down, Neptune back to usbhid, display wake, backlight)
python3 -m deckgadget run --help                       # all flags (-v for debug logging, --udc to force a UDC)
```

`run`/`demo` print JSON‑lines events on **stdout** (`state`, `error`, `metrics`, `kill`, `screen`) and
human‑readable logs on **stderr**; Ctrl+C / SIGTERM performs a graceful rollback and exits 0. While `run` is
active the Deck's own controller is captured — exit with the kill combo (default L4+R4 held 1.5 s), Ctrl+C in
the SSH session, or unplug the cable. If anything looks stuck: `sudo python3 -m deckgadget recover` (or reboot —
all state is volatile).

Not root? `status` still works (DRD/UDC/extcon are world‑readable); everything else needs root for
sysfs unbind, usbfs and `/dev/raw-gadget`.

### Screen off (`--screen-off`, `--screen-method`)

`--screen-method auto|gamescope|kscreen|backlight` (default `auto`; the backend does not pass it, so it
always uses `auto`). Strategies, tried in this order by `auto` — the first one whose sleep succeeds is kept for
the whole session (touch wake / re‑sleep / final wake all use the same one):

| method | when | what runs | note |
|---|---|---|---|
| `gamescope` | Gaming Mode: socket `/run/user/1000/gamescope-0` exists | `gamescopectl drm_sleep_internal_screen 1` / `0` with `XDG_RUNTIME_DIR=/run/user/1000 GAMESCOPE_WAYLAND_DISPLAY=gamescope-0` (as root; 3 s timeout) | panel really off — what Steam's idle "turn off screen" does |
| `kscreen` | Desktop Mode: `/run/user/1000/wayland-0` exists and `kscreen-doctor` is on PATH | `kscreen-doctor --dpms off` / `on` as uid/gid 1000 with `XDG_RUNTIME_DIR`, `WAYLAND_DISPLAY=wayland-0`, `DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus` | optional; DPMS off via KWin |
| `backlight` | always (last resort) | `/sys/class/backlight/amdgpu_bl0/brightness` = 0, previous value saved to `/run/deckgadget/brightness` | on the OLED this only **dims** to minimum (verified on the device) |

When gamescope or kscreen is in charge the backlight is never touched. The daemon emits
`{"ev":"screen","off":bool,"method":"gamescope|kscreen|backlight|none"}`; `deckgadget status` reports
`"screen_methods": {"gamescope": bool, "kscreen": bool, "backlight": bool}` (what would work right now) and
`"gamescope_socket"`. `recover` always does a best‑effort `gamescopectl drm_sleep_internal_screen 0` /
`kscreen-doctor --dpms on` when the respective compositor is reachable (reported under `"display"`, failures
are `"warnings"`, not errors), then restores the backlight as before — so a crashed session can never leave the
panel asleep.

How to test on the Deck (Gaming Mode, SSH in as `deck`):

```sh
cd ~/homebrew/plugins/decky-controller/py_modules
sudo python3 -m deckgadget status | grep -A3 screen_methods   # expect "gamescope": true in Gaming Mode
sudo python3 -m deckgadget run --profile xbox360 --screen-off --log-file /tmp/deckgadget.log
#   -> the panel must go completely dark (not dim); stdout shows {"ev":"screen","off":true,"method":"gamescope"}
#   -> touch the screen: it wakes for --touch-wake-seconds, then sleeps again; L4+R4 / Ctrl+C -> screen back on
sudo python3 -m deckgadget run --profile xbox360 --screen-off --screen-method backlight   # force the dim path
# manual wake if anything is left asleep (both idempotent):
sudo python3 -m deckgadget recover
sudo env XDG_RUNTIME_DIR=/run/user/1000 GAMESCOPE_WAYLAND_DISPLAY=gamescope-0 gamescopectl drm_sleep_internal_screen 0
```

In Desktop Mode `gamescopectl` prints `Failed to open GAMESCOPE_WAYLAND_DISPLAY.` (rc 1, no socket) and `auto`
falls through to `kscreen` / `backlight`. A gamescope without the ConVar answers `Command not found.` with rc
**0** (checked with gamescope 3.16); the daemon treats that output as a failure too, so it never believes the
panel is asleep when it is not. When no strategy works the daemon emits `{"ev":"screen","off":false,"method":"none"}`.

## Logs and runtime files

`~/homebrew/{plugins,logs,settings,data}` is Decky Loader's directory on the Deck. Decky derives
`DECKY_PLUGIN_LOG_DIR` / `DECKY_PLUGIN_SETTINGS_DIR` / `DECKY_PLUGIN_RUNTIME_DIR` from the installed plugin
**directory** name (`~/homebrew/plugins/decky-controller`, the zip's top‑level folder), not from
`plugin.json`'s `name` — hence `decky-controller` below, not `Decky Controller`.

* Decky backend log (everything the backend logs, plus every daemon stdout/stderr line prefixed `[deckgadget]`):
  `~/homebrew/logs/decky-controller/` (Decky → Settings → Developer → *Show plugin logs* shows the same), and
  `journalctl -u plugin_loader` for the loader itself.
* Daemon log file (`--log-file`, written by the backend‑started daemon):
  `~/homebrew/logs/decky-controller/deckgadget.log`.
* Settings: `~/homebrew/settings/decky-controller/settings.json` (JSON, edited through `set_settings`).
* Runtime: pidfile `~/homebrew/data/decky-controller/deckgadget.pid` (used to kill a stale daemon at load);
  saved backlight value `/run/deckgadget/brightness` (fallback `/tmp/deckgadget/brightness`) while a session
  uses the backlight method — `recover` restores and deletes it (gamescope/kscreen sleep keeps no state:
  `recover` simply issues the wake command when the compositor socket exists).
* In Gaming Mode the **Diagnostics** button in the panel dumps `deckgadget status`, versions, settings,
  daemon info and the last 50 log / stdout lines.

## Backend ↔ daemon ↔ frontend contract checks

When touching any side, keep these aligned (see [ARCHITECTURE.md](ARCHITECTURE.md)):

* callables `get_status`, `start(profile)`, `stop`, `get_settings`, `set_settings(settings)`,
  `get_diagnostics` — `src/api.ts` ↔ `main.py:Plugin`;
* events `status` (Status) and `toast` ({title, body, severity}) — `src/index.tsx` ↔
  `py_modules/controller_backend/service.py`;
* daemon CLI flags — `py_modules/controller_backend/daemon/launcher.py:run_args` ↔
  `py_modules/deckgadget/__main__.py:_add_run_args`;
* daemon stdout events — `py_modules/deckgadget/util/log.py:JsonEventSink` ↔
  `py_modules/controller_backend/session.py:SessionView.apply`;
* allowed values (profiles, transports, kill combos, paddle targets) — `py_modules/controller_backend/settings.py` ↔
  `py_modules/deckgadget/config.py` ↔ `src/types.ts`.
