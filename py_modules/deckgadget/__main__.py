"""python3 -m deckgadget run|demo|status|recover|probe

run      capture the Deck controller and expose it to the PC (JSON-lines events on stdout)
demo     same with a synthetic source (no capture)
status   JSON snapshot: DRD / UDC / cable / Neptune / gadgets / screen
recover  idempotent full rollback, always exits 0
probe    capture the controller and print decoded reports
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from deckgadget import __version__
from deckgadget import config as C
from deckgadget.util.log import JsonEventSink, get_logger, setup_logging

log = get_logger("cli")


def _add_run_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--profile", choices=C.PROFILES, default=C.DEFAULT_PROFILE)
    parser.add_argument("--transport", choices=C.TRANSPORTS, default=C.DEFAULT_TRANSPORT)
    parser.add_argument("--kill-combo", default=C.DEFAULT_KILL_COMBO, help="one of %s" % ", ".join(C.KILL_COMBOS))
    parser.add_argument("--kill-hold-ms", type=int, default=C.DEFAULT_KILL_HOLD_MS)
    parser.add_argument("--screen-off", action="store_true", default=False)
    parser.add_argument("--touch-wake-seconds", type=float, default=C.DEFAULT_TOUCH_WAKE_SECONDS)
    parser.add_argument("--screen-method", choices=C.SCREEN_METHODS, default=C.DEFAULT_SCREEN_METHOD,
                   help="how to turn the screen off: auto = gamescope display sleep (Gaming Mode) -> "
                        "kscreen-doctor DPMS (Desktop Mode) -> backlight 0 (dims only)")
    parser.add_argument("--paddles", default=None, help="L4=none,L5=none,R4=none,R5=none")
    parser.add_argument("--forward-steam", action="store_true", default=False, help="map Steam -> Guide")
    parser.add_argument("--forward-qam", action="store_true", default=False, help="map QAM -> Guide")
    parser.add_argument("--udc", default=None, help="UDC name (default: first in /sys/class/udc)")
    parser.add_argument("--log-file", default=None)
    parser.add_argument("-v", "--verbose", action="store_true", default=False)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="deckgadget", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=f"deckgadget {__version__}")
    subparsers = parser.add_subparsers(dest="cmd", required=True)
    _add_run_args(subparsers.add_parser("run", help="run the controller session"))
    _add_run_args(subparsers.add_parser("demo", help="run with a synthetic input source"))
    status_parser = subparsers.add_parser("status", help="print a JSON status snapshot")
    status_parser.add_argument("--no-modprobe", action="store_true", help="skip modprobe -R based DRD detection")
    recover_parser = subparsers.add_parser("recover", help="idempotent full rollback (exit 0)")
    recover_parser.add_argument("--log-file", default=None)
    probe_parser = subparsers.add_parser("probe", help="capture the controller and print decoded reports")
    probe_parser.add_argument("--seconds", type=float, default=10.0)
    probe_parser.add_argument("--all", action="store_true", help="print every report, not only on change")
    probe_parser.add_argument("--json", action="store_true", help="machine-readable output")
    probe_parser.add_argument("--sensors", action="store_true", help="include gyro/accel/pads")
    probe_parser.add_argument("-v", "--verbose", action="store_true", default=False)
    return parser


def config_from_args(args: argparse.Namespace, demo: bool = False) -> C.RunConfig:
    return C.RunConfig(
        profile=args.profile, transport=args.transport, kill_combo=args.kill_combo,
        kill_hold_ms=args.kill_hold_ms, screen_off=bool(args.screen_off),
        touch_wake_seconds=args.touch_wake_seconds,
        screen_method=getattr(args, "screen_method", C.DEFAULT_SCREEN_METHOD),
        paddles=C.parse_paddles(args.paddles),
        log_file=args.log_file, demo=demo, udc=args.udc,
        forward_steam=bool(args.forward_steam), forward_qam=bool(args.forward_qam),
    )


def collect_status(sysfs: str = "/sys", dev: str = "/dev", use_modprobe: bool = True, *,
                   configfs: Optional[str] = None, run_user_base: Optional[str] = None,
                   state_file: Optional[str] = None) -> Dict[str, Any]:
    """The ``status`` JSON. Roots default to the real system; tests point every one of them at a fake tree."""
    from deckhw.facts import hardware_facts

    from deckgadget.platform import guard
    from deckgadget.platform.display.backlight import BACKLIGHT_NAME, Backlight
    from deckgadget.platform.display.base import default_state_file
    from deckgadget.platform.display.compositor import DECK_UID, RUN_USER_BASE, GamescopeSleep, KscreenDpms
    from deckgadget.platform.display.touch import find_touchscreen

    configfs = configfs or guard.CONFIGFS
    run_user_base = run_user_base or RUN_USER_BASE
    state_file = state_file or default_state_file()
    out: Dict[str, Any] = {"ok": True, "version": __version__, "errors": [], "root": os.geteuid() == 0}
    try:
        out.update(hardware_facts(sysfs, dev, use_modprobe=use_modprobe))
    except Exception as exc:  # noqa: BLE001
        out["errors"].append(f"hardware: {exc}")
        out.update({"neptune_present": False, "neptune_captured": False})
    try:
        out["gadgets"] = guard.list_gadgets(configfs)
    except Exception as exc:  # noqa: BLE001
        out["errors"].append(f"gadgets: {exc}")
    backlight_available = False
    try:
        backlight = Backlight(os.path.join(sysfs, "class", "backlight", BACKLIGHT_NAME), state_file)
        backlight_available = backlight.available
        out["backlight"] = {"available": backlight_available,
                            "brightness": backlight.brightness() if backlight_available else None,
                            "max": backlight.max_brightness() if backlight_available else None,
                            "saved": backlight.saved_value(), "state_file": backlight.state_file}
        out["screen_off"] = bool(backlight_available and backlight.brightness() == 0
                                 and backlight.saved_value() is not None)
        out["touchscreen"] = find_touchscreen(sysfs, dev)
    except Exception as exc:  # noqa: BLE001
        out["errors"].append(f"screen: {exc}")
    try:
        gamescope = GamescopeSleep(run_user_base=run_user_base)
        kscreen = KscreenDpms(runtime_dir=os.path.join(run_user_base, str(DECK_UID)))
        # what would work right now; auto tries gamescope -> kscreen -> backlight
        out["screen_methods"] = {"gamescope": gamescope.available(), "kscreen": kscreen.available(),
                                 "backlight": backlight_available}
        out["gamescope_socket"] = gamescope.socket_path
    except Exception as exc:  # noqa: BLE001
        out["errors"].append(f"screen_methods: {exc}")
    out["ok"] = not out["errors"]
    return out


def cmd_status(args: argparse.Namespace) -> int:
    print(json.dumps(collect_status(use_modprobe=not args.no_modprobe), indent=2, ensure_ascii=False))
    return 0


def cmd_recover(args: argparse.Namespace) -> int:
    from deckgadget.platform import guard

    setup_logging(logging.INFO, getattr(args, "log_file", None))
    try:
        report = guard.recover()
    except Exception as exc:  # noqa: BLE001 - recover must never fail loudly
        log.error("recover crashed: %s", exc, exc_info=True)
        report = {"ok": False, "errors": [str(exc)]}
    print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    return 0


def cmd_run(args: argparse.Namespace, demo: bool = False) -> int:
    from deckgadget.session import build_session

    setup_logging(logging.DEBUG if args.verbose else logging.INFO, args.log_file)
    events = JsonEventSink(sys.stdout)
    try:
        config = config_from_args(args, demo=demo)
    except C.ConfigError as exc:
        events.error(f"config: {exc}")
        print(f"error: {exc}", file=sys.stderr)
        return 2
    log.info("deckgadget %s %s: %s", __version__, "demo" if demo else "run", json.dumps(config.as_dict()))
    if os.geteuid() != 0:
        log.warning("not running as root; capture/gadget operations will fail")
    session = build_session(config, events)

    def on_signal(signum, _frame) -> None:
        log.info("signal %s -> stopping", signal.Signals(signum).name)
        session.request_stop("signal")

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGHUP, on_signal)
    exit_code = session.run()
    _report_leftovers()
    return exit_code


def _report_leftovers(sysfs: str = "/sys", dev: str = "/dev", configfs: Optional[str] = None) -> None:
    """After the session's own best-effort teardown: anything still captured or bound is an ERROR (the Decky
    backend runs ``recover`` anyway; a standalone run must not look clean when it is not)."""
    from deckhw.neptune import find_neptune

    from deckgadget.platform import guard

    device = find_neptune(sysfs, dev)
    if device is not None and device.captured:
        log.error("teardown left the built-in controller detached from usbhid — run `deckgadget recover`")
    gadgets = guard.list_gadgets(configfs or guard.CONFIGFS)
    if gadgets:
        log.error("teardown left configfs gadget(s) %s — run `deckgadget recover`", ", ".join(gadgets))


def cmd_probe(args: argparse.Namespace) -> int:
    from deckgadget.sources.neptune.source import NeptuneUsbSource

    setup_logging(logging.DEBUG if args.verbose else logging.INFO)
    if os.geteuid() != 0:
        print("probe needs root (usbfs + sysfs unbind)", file=sys.stderr)
    source = NeptuneUsbSource(with_sensors=args.sensors)
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda signum, frame: stop.set())
    signal.signal(signal.SIGTERM, lambda signum, frame: stop.set())
    try:
        source.open()
        print(f"probe: device {source.device.name if source.device else '?'} ep 0x{source.ep_in:02x}; "
              f"press buttons — {args.seconds:.0f}s (Ctrl+C to stop). Kill combo is NOT active here.",
              file=sys.stderr, flush=True)
        state_reports, other_packets = _probe_capture(source, args, stop)
        print(f"probe done: {state_reports} state reports, {other_packets} other packets, "
              f"heartbeats={source.heartbeats}", file=sys.stderr, flush=True)
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"probe failed: {exc}", file=sys.stderr)
        return 1
    finally:
        source.close()


def _probe_capture(source, args: argparse.Namespace, stop: threading.Event) -> Tuple[int, int]:
    """Read raw reports until ``--seconds`` elapse or ``stop`` is set; print a line when the buttons change or
    every 0.5 s (every report with ``--all``). Returns (state reports, other packets)."""
    from deckgadget.sources.neptune.protocol import decode_report, parse_report

    deadline = time.monotonic() + args.seconds
    last_buttons: Optional[int] = None
    last_summary = 0.0
    state_reports = 0
    other_packets = 0
    while not stop.is_set() and time.monotonic() < deadline:
        raw = source.read_raw(0.1)
        if raw is None:
            continue
        decoded = decode_report(raw)
        if not decoded.get("deck_state"):
            other_packets += 1
            if args.all:
                print(f"[other type={decoded.get('type')}] {raw.hex()}", flush=True)
            continue
        state_reports += 1
        state = parse_report(raw, time.monotonic(), with_sensors=args.sensors)
        buttons = state.buttons if state else 0
        changed = buttons != last_buttons
        now = time.monotonic()
        if args.all or changed or now - last_summary >= 0.5:
            _print_probe_report(raw, decoded, state, changed, args)
            last_summary = now
            last_buttons = buttons
    return state_reports, other_packets


def _print_probe_report(raw: bytes, decoded: Dict[str, Any], state, changed: bool, args: argparse.Namespace) -> None:
    from deckgadget.state import button_names

    if args.json:
        print(json.dumps({"raw": raw.hex(), "decoded": decoded, "canonical": state.as_dict() if state else None},
                         ensure_ascii=False, default=str), flush=True)
        return
    if changed or args.all:
        print(f"raw: {raw.hex()}")
        print(f"  packet={decoded['packet']} L={decoded['buttons_l']} H={decoded['buttons_h']} "
              f"bits={decoded['buttons']} unknown={decoded['unknown_bits']}")
        print(f"  canonical={button_names(state.buttons if state else 0)}")
    sensors = (f" lpad={decoded['lpad']} rpad={decoded['rpad']} gyro={decoded['gyro']} accel={decoded['accel']}"
               if args.sensors else "")
    print(f"  sticks L={decoded['lstick']} R={decoded['rstick']} trig L={decoded['trigger_l']} R={decoded['trigger_r']}"
          + sensors, flush=True)


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "run":
        return cmd_run(args, demo=False)
    if args.cmd == "demo":
        return cmd_run(args, demo=True)
    if args.cmd == "status":
        return cmd_status(args)
    if args.cmd == "recover":
        return cmd_recover(args)
    if args.cmd == "probe":
        return cmd_probe(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
