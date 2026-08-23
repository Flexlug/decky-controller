# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Decky Loader plugin that turns a Steam Deck into a USB gamepad (XInput / generic HID) for another PC via the
USB‑C port. Three parts that talk over a fixed contract:

```
src/ (React QAM panel)  --callables/events-->  main.py (Decky backend, root)
                                                  |  subprocess: /usr/bin/python3 -m deckgadget run …  (JSON-lines on stdout)
                                                  v
                              py_modules/deckgadget (daemon: capture built-in controller -> profile.pack -> USB gadget)
```

The full contract (callables, `Status`/`Settings` shapes, daemon CLI flags, stdout events, state machine) lives
in `docs/ARCHITECTURE.md`; workflows in `docs/DEV.md`; verified hardware facts in `docs/HARDWARE.md`. Read
ARCHITECTURE.md before touching anything that crosses a boundary. `notes/` is gitignored local drafts — not
part of the repo or the release.

## Commands

`pnpm` 9 is the package manager (`package.json` → `packageManager`). If `pnpm` is not on PATH, every command
below works as `corepack pnpm@9 <cmd>` (or `npx pnpm@9 <cmd>`).

```sh
pnpm install --frozen-lockfile
pnpm run build          # rollup -> dist/index.js (the only frontend artifact that ships)
pnpm run watch          # rebuild on change
pnpm run typecheck      # tsc --noEmit
pnpm test               # python3 -m compileall -q py_modules main.py && python3 -m unittest discover -s tests -v
pnpm run zip            # scripts/build-zip.sh -> out/decky-controller.zip (add --no-build to skip the frontend build)
```

Python tests need no hardware and no deps (stdlib `unittest`, Python ≥ 3.11). Every test file starts with
`import _path` (puts `py_modules/` on `sys.path`), so they must be run via discover from `tests/`, **not** as
`python3 -m unittest tests.test_x` (that form fails on the `_path` import):

```sh
python3 -m unittest discover -s tests -p 'test_config.py' -v            # one file
python3 -m unittest discover -s tests -p 'test_config.py' -k test_kill_combo   # one test
```

Off‑Deck sanity checks that work on any Linux box: `python3 -c "import main"` (a built‑in `decky` shim makes
the backend importable) and `(cd py_modules && python3 -m deckgadget status)` (read‑only JSON snapshot).

On the Deck (as root, from `~/homebrew/plugins/decky-controller/py_modules` or the repo's `py_modules/`):
`python3 -m deckgadget run|demo|status|recover|probe` — `run`/`demo`/`probe` capture the controller and/or
bring up a gadget; `recover` is the idempotent undo‑everything; `status` is the only one that works unprivileged.
See `docs/DEV.md` for flags, logs and the screen‑off test procedure.

Release: bump `version` in `package.json`, commit, `git tag v<version>`, push the tag. CI
(`.github/workflows/build.yml`) refuses to release if the tag ≠ `v` + package.json version.

## Architecture — what you need to hold in your head

* **Frontend** (`src/`): `index.tsx` is the `definePlugin` entry — it subscribes to the backend's `status` and
  `toast` events for the whole session and shows/hides the ACTIVE modal; `Content.tsx` is the panel;
  `actions.ts` calls the backend (`api.ts` typed `callable` wrappers) and pushes results into `store.ts`
  (module‑level store that survives panel unmounts); `types.ts` mirrors the backend enums/defaults.
* **Backend** (`main.py`, one file): `class Plugin` exposes the six callables (`get_status`, `start`, `stop`,
  `get_settings`, `set_settings`, `get_diagnostics`) — all async, all return JSON dicts, none raise (errors
  come back as `{"ok": false, "error": …}`). `_Backend` is the daemon supervisor: spawns
  `python3 -m deckgadget run …` with `cwd=<plugin>/py_modules` and `LD_LIBRARY_PATH` stripped, pumps its
  stdout JSON‑lines into `status` events (`_on_daemon_event`), runs a 2 s status loop, and owns the settings
  store. **`main.py` never imports `deckgadget`** — only executes it as a subprocess, so a broken core cannot
  take the backend down. Allowed values (`PROFILES`, `KILL_COMBOS`, `PADDLE_ACTIONS`, …) are duplicated as
  constants near the top of the file.
* **Daemon** (`py_modules/deckgadget/`): `__main__.py` (CLI) → `config.py` (single source of truth for allowed
  values + validation) → `session.py` (state machine `IDLE → CAPTURING → GADGET_UP → WAITING_HOST → ACTIVE →
  STOPPING`, kill‑combo hold detector, unplug detection, hot loop). Pluggable protocols:
  `sources/` (`InputSource`: `neptune_usb.py` = exclusive usbfs capture of the built‑in controller,
  `demo.py` = synthetic), `profiles/` (`Profile`: USB descriptors + `pack(ControllerState) -> bytes`;
  `xbox360.py`, `hid_gamepad.py`), `transports/` (`Transport`: `usb_raw_gadget.py` for xbox360 via
  `/dev/raw-gadget`, `usb_hid.py` for configfs `f_hid`). `platform/` is read‑only hardware inspection
  (`usb_role.py`), Neptune bind/unbind (`neptune.py`), screen sleep strategies (`screen.py`) and
  `guard.py:recover()`. `state.py` is the canonical `ControllerState` with its own button numbering.
* **Rollback is the invariant.** Every exit path (kill combo, unplug, signal, exception, `stop`, unload,
  uninstall, backend start after a crash) ends in `guard.recover()`: delete configfs gadgets → rebind Neptune
  to `usbhid` → wake display → restore backlight. The backend also runs it at load, after *every* daemon exit
  and inside `stop()` (SIGTERM → 3 s → SIGKILL → recover), under one lock with `start`. Nothing persistent is
  written outside Decky's settings/log dirs; a reboot restores everything.
* `transport=auto` resolves to `raw` for xbox360 and `hid` for hid_gamepad; xbox360 over `hid` is rejected
  (`f_hid` cannot expose the vendor interface).

## Hard constraints (from ARCHITECTURE.md — non‑negotiable)

* Backend and daemon: **Python standard library + ctypes only** (SteamOS has no pip). Frontend deps are bundled
  by rollup. Target SteamOS 3.6+ / Python 3.11+ (dev Deck runs 3.13).
* **Never switch the USB port role yourself** (no PCI unbind/bind, no debugfs `mode` writes) — the Valve EC
  driver / extcon / dwc3 does it. The daemon only *observes* extcon and `/sys/class/udc/*`.
* **No code copied from GPL projects** (hid‑steam, InputPlumber, HHD, GP2040‑CE, …) — facts and constants only,
  with the source cited in a comment. SDL (zlib) is the protocol reference for the Deck's HID reports.
* Frontend text budget: status‑row values and dropdown option labels ≤ 14 characters; never show volts, amps or
  the words "PD contract" in the UI (the `cable_*`/`pd_contract_*` fields exist for classification and
  diagnostics only).
* Contract names (callables, event names, `Status`/`Settings` keys, CLI flags, enum values, module paths) are
  not renamed casually. When changing one, update all sides together:
  - callables — `src/api.ts` ↔ `main.py:Plugin`
  - events `status` / `toast` — `src/index.tsx` ↔ `main.py`
  - daemon CLI flags — `main.py:_Backend._daemon_args` ↔ `py_modules/deckgadget/__main__.py:_add_run_args`
  - daemon stdout events — `py_modules/deckgadget/util/log.py:JsonEventSink` ↔ `main.py:_on_daemon_event`
  - allowed values — `py_modules/deckgadget/config.py` (source of truth) ↔ `main.py` constants ↔ `src/types.ts`

## Code style rules (project‑wide, from review)

* **Self‑documenting code first; comments to an absolute minimum** without hurting readability. A short
  docstring per function/class is fine; no walls of text, and never a comment that restates the name
  (`extract_string` — "extracts a string"). Keep only comments that carry non‑obvious facts (hardware
  quirks, kernel behaviour, protocol constants with their source).
* **No references to docs/architecture/ADR items in code.** Never write "see docs/ARCHITECTURE.md",
  "step 4 of recover()", "section X" in comments or docstrings — it is noise that rots. Code explains
  itself; the docs explain the system.
* **Descriptive variable names; no acronyms or abbreviations** (`st`, `rc`, `pr`, `ap`, `bl`, `v`, `t`,
  `d`, `m` …). Single‑letter names only where convention makes them idiomatic (loop indices `i`/`j`,
  `f` in `with open(...) as f`, `e` in `except ... as e`).
* **No duplicated helpers.** One implementation of `_read`/`_write`‑style utilities shared across modules
  (`deckgadget/util/`), one fake‑sysfs builder shared across tests — not a private copy per file.

## Testing conventions

* `tests/fakes.py` is the shared toolbox: `FakeSysfs(root)` builds a fake `/sys` + `/dev` + configfs tree with
  chainable `add_neptune() / add_backlight() / add_power_supply() / add_hwmon() / add_extcon() / add_udc() /
  add_gadget()` (layouts mirror a real Deck OLED), plus `write`, `read`, `make_socket`. Don't write private
  copies in a test file.
* Everything that touches sysfs/configfs/usbfs/`/dev` takes injectable root paths as keyword args
  (`guard.recover(sysfs=…, configfs=…, dev=…)`, `main._sysfs_snapshot(sysfs=…)`, `NeptuneUsbSource(...,
  device_class=…)`, `UsbHidTransport(configfs=…, sysfs=…, dev=…, modprobe=False)`); collaborators are swapped
  with `mock.patch.object` or injected fakes (`FakeRawGadgetDevice`, `FakeUsbfsDevice`, screen methods,
  `_Backend._run_cli`). Keep new code injectable the same way — never spawn the real daemon or touch the real
  `/sys` in a test.
* `tests/_path.py` (imported first in every test) puts `py_modules/` on `sys.path` and attaches a root
  `NullHandler` so the daemon/backend loggers stay quiet; use `assertLogs` when a log line is the assertion.
* What is covered (and where): config/CLI (`test_config`), report parser + feature commands
  (`test_neptune_parser`), capture lifecycle with a fake usbfs device (`test_neptune_source`), profiles
  (`test_profiles`), session state machine (`test_session`), raw-gadget EP0 + lifecycle + `ReportSlot`
  (`test_raw_gadget`), f_hid configfs transport (`test_usb_hid`), `recover()` (`test_guard`), screen
  strategies (`test_screen`), cable classification (`test_usb_role`), usbfs ABI sizes / `_IOC`
  (`test_ioctl`), the Decky backend (`test_main`), and the three-sided contract — value lists, Status/Settings
  shapes, callables, events — across `config.py` ↔ `main.py` ↔ `src/*.ts` (`test_contract`). Not unit-testable
  and left to hands-on checks on the Deck: real usbfs/raw-gadget/extcon, gamescope, the Windows host, the
  `@decky/ui` panel.
* Tests are constant-free: assert behaviour, not `CONSTANT == literal` (the only literals pinned are kernel
  ABI facts such as ctypes struct sizes). Keep the suite fast (whole run ≈ 2 s) — synchronise threads with
  `Event`/`Condition`, never sleep-poll.

## Packaging facts that bite

* `scripts/build-zip.sh` ships only `dist/index.js`, `main.py`, `plugin.json`, `package.json`, `py_modules/`
  (minus `__pycache__`), `LICENSE`, `README.md`, `THIRD_PARTY_NOTICES.md` under a top‑level `decky-controller/`
  folder, and fails if a `.map`, `notes/`, `node_modules/` or `__pycache__` sneaks in.
* Decky derives `DECKY_PLUGIN_{LOG,SETTINGS,RUNTIME}_DIR` from that folder name (`decky-controller`), not from
  `plugin.json`'s `"name"` — so runtime paths are `~/homebrew/{logs,settings,data}/decky-controller/`.
* `plugin.json` has `"flags": ["root"]` — the backend and daemon must run as root (usbfs, raw‑gadget,
  configfs, sysfs bind/unbind, backlight are all root‑only).
