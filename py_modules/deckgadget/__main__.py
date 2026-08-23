"""CLI entry point: ``python3 -m deckgadget run|status|recover|probe|demo`` (docs/ARCHITECTURE.md).

* ``run``     — capture the Deck controller and expose it to the PC; JSON-lines events on stdout
* ``demo``    — like ``run`` but with a synthetic source (no controller capture)
* ``status``  — JSON snapshot: DRD / UDC / extcon / cable (power, PD contract, kind) / Neptune / captured /
                gadgets / screen (+ screen_methods)
* ``recover`` — idempotent full rollback (incl. waking the display), always exits 0
* ``probe``   — capture the controller for N seconds and print decoded reports (bit calibration)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
from typing import Any, Dict, List, Optional

from . import __version__
from . import config as C
from .util.log import JsonEventSink, get_logger, setup_logging

log = get_logger("cli")


def _add_run_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--profile", choices=C.PROFILES, default=C.DEFAULT_PROFILE)
    p.add_argument("--transport", choices=C.TRANSPORTS, default=C.DEFAULT_TRANSPORT)
    p.add_argument("--kill-combo", default=C.DEFAULT_KILL_COMBO, help="one of %s" % ", ".join(C.KILL_COMBOS))
    p.add_argument("--kill-hold-ms", type=int, default=C.DEFAULT_KILL_HOLD_MS)
    p.add_argument("--screen-off", action="store_true", default=False)
    p.add_argument("--touch-wake-seconds", type=float, default=C.DEFAULT_TOUCH_WAKE_SECONDS)
    p.add_argument("--screen-method", choices=C.SCREEN_METHODS, default=C.DEFAULT_SCREEN_METHOD,
                   help="how to turn the screen off: auto = gamescope display sleep (Gaming Mode) -> "
                        "kscreen-doctor DPMS (Desktop Mode) -> backlight 0 (dims only)")
    p.add_argument("--paddles", default=None, help="L4=none,L5=none,R4=none,R5=none")
    p.add_argument("--forward-steam", action="store_true", default=False, help="map Steam -> Guide")
    p.add_argument("--forward-qam", action="store_true", default=False, help="map QAM -> Guide")
    p.add_argument("--udc", default=None, help="UDC name (default: first in /sys/class/udc)")
    p.add_argument("--log-file", default=None)
    p.add_argument("-v", "--verbose", action="store_true", default=False)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="deckgadget", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"deckgadget {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)
    _add_run_args(sub.add_parser("run", help="run the controller session"))
    _add_run_args(sub.add_parser("demo", help="run with a synthetic input source"))
    st = sub.add_parser("status", help="print a JSON status snapshot")
    st.add_argument("--no-modprobe", action="store_true", help="skip modprobe -R based DRD detection")
    rc = sub.add_parser("recover", help="idempotent full rollback (exit 0)")
    rc.add_argument("--log-file", default=None)
    pr = sub.add_parser("probe", help="capture the controller and print decoded reports")
    pr.add_argument("--seconds", type=float, default=10.0)
    pr.add_argument("--all", action="store_true", help="print every report, not only on change")
    pr.add_argument("--json", action="store_true", help="machine-readable output")
    pr.add_argument("--sensors", action="store_true", help="include gyro/accel/pads")
    pr.add_argument("-v", "--verbose", action="store_true", default=False)
    return ap


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


# --------------------------------------------------------------------------------------
# status
# --------------------------------------------------------------------------------------

def _read(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return None


def collect_status(sysfs: str = "/sys", dev: str = "/dev", use_modprobe: bool = True) -> Dict[str, Any]:
    from .platform import guard, neptune, screen, usb_role

    out: Dict[str, Any] = {"ok": True, "version": __version__, "errors": []}
    out["kernel"] = os.uname().release
    out["model"] = _read(os.path.join(sysfs, "class", "dmi", "id", "product_name"))
    out["root"] = (os.geteuid() == 0)
    try:
        out.update(usb_role.usb_role_status(sysfs, dev, use_modprobe=use_modprobe).as_dict())
    except Exception as exc:  # noqa: BLE001
        out["errors"].append(f"usb_role: {exc}")
    try:
        n = neptune.find_neptune(sysfs, dev)
        out["neptune_present"] = n is not None
        out["neptune_captured"] = bool(n and n.captured)
        out["neptune"] = n.as_dict() if n else None
    except Exception as exc:  # noqa: BLE001
        out["errors"].append(f"neptune: {exc}")
        out["neptune_present"] = False
        out["neptune_captured"] = False
    try:
        out["gadgets"] = guard.list_gadgets()
    except Exception as exc:  # noqa: BLE001
        out["errors"].append(f"gadgets: {exc}")
    try:
        bl = screen.Backlight()
        out["backlight"] = {"available": bl.available, "brightness": bl.brightness() if bl.available else None,
                            "max": bl.max_brightness() if bl.available else None, "saved": bl.saved_value(),
                            "state_file": bl.state_file}
        out["screen_off"] = bool(bl.available and bl.brightness() == 0 and bl.saved_value() is not None)
        out["touchscreen"] = screen.find_touchscreen(sysfs, dev)
    except Exception as exc:  # noqa: BLE001
        out["errors"].append(f"screen: {exc}")
    try:
        gs = screen.GamescopeSleep()
        ks = screen.KscreenDpms()
        bl_avail = bool(out.get("backlight", {}).get("available")) if isinstance(out.get("backlight"), dict) else False
        # Which screen-off strategies would work right now (auto order: gamescope -> kscreen -> backlight).
        out["screen_methods"] = {"gamescope": gs.available(), "kscreen": ks.available(), "backlight": bl_avail}
        out["gamescope_socket"] = gs.socket_path
    except Exception as exc:  # noqa: BLE001
        out["errors"].append(f"screen_methods: {exc}")
    out["ok"] = not out["errors"]
    return out


def cmd_status(args: argparse.Namespace) -> int:
    print(json.dumps(collect_status(use_modprobe=not args.no_modprobe), indent=2, ensure_ascii=False))
    return 0


# --------------------------------------------------------------------------------------
# recover
# --------------------------------------------------------------------------------------

def cmd_recover(args: argparse.Namespace) -> int:
    from .platform import guard

    setup_logging(logging.INFO, getattr(args, "log_file", None))
    try:
        report = guard.recover()
    except Exception as exc:  # noqa: BLE001 - recover must never fail loudly
        report = {"ok": False, "errors": [str(exc)]}
    print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    return 0


# --------------------------------------------------------------------------------------
# run / demo
# --------------------------------------------------------------------------------------

def cmd_run(args: argparse.Namespace, demo: bool = False) -> int:
    from .session import build_session

    setup_logging(logging.DEBUG if args.verbose else logging.INFO, args.log_file)
    events = JsonEventSink(sys.stdout)
    try:
        cfg = config_from_args(args, demo=demo)
    except C.ConfigError as exc:
        events.error(f"config: {exc}")
        print(f"error: {exc}", file=sys.stderr)
        return 2
    log.info("deckgadget %s %s: %s", __version__, "demo" if demo else "run", json.dumps(cfg.as_dict()))
    if os.geteuid() != 0:
        log.warning("not running as root; capture/gadget operations will fail")
    session = build_session(cfg, events)

    def on_signal(signum, _frame) -> None:
        log.info("signal %s -> stopping", signal.Signals(signum).name)
        session.request_stop("signal")

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGHUP, on_signal)
    return session.run()


# --------------------------------------------------------------------------------------
# probe
# --------------------------------------------------------------------------------------

def cmd_probe(args: argparse.Namespace) -> int:
    from .sources.neptune_usb import NeptuneUsbSource, decode_report, parse_report
    from .state import button_names

    setup_logging(logging.DEBUG if args.verbose else logging.INFO)
    if os.geteuid() != 0:
        print("probe needs root (usbfs + sysfs unbind)", file=sys.stderr)
    src = NeptuneUsbSource(with_sensors=args.sensors)
    stop = {"flag": False}

    def on_signal(signum, _frame) -> None:
        stop["flag"] = True

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)
    emit = (lambda obj: print(json.dumps(obj, ensure_ascii=False, default=str), flush=True)) if args.json else None
    try:
        src.open()
        print(f"probe: device {src.device.name if src.device else '?'} ep 0x{src.ep_in:02x}; "
              f"press buttons — {args.seconds:.0f}s (Ctrl+C to stop). Kill combo is NOT active here.",
              file=sys.stderr, flush=True)
        deadline = time.monotonic() + args.seconds
        last_buttons: Optional[int] = None
        last_summary = 0.0
        count = 0
        other = 0
        while not stop["flag"] and time.monotonic() < deadline:
            raw = src.read_raw(0.1)
            if raw is None:
                continue
            dec = decode_report(raw)
            if not dec.get("deck_state"):
                other += 1
                if args.all:
                    print(f"[other type={dec.get('type')}] {raw.hex()}", flush=True)
                continue
            count += 1
            st = parse_report(raw, time.monotonic(), with_sensors=args.sensors)
            buttons = st.buttons if st else 0
            changed = buttons != last_buttons
            now = time.monotonic()
            if args.all or changed or now - last_summary >= 0.5:
                if emit:
                    emit({"raw": raw.hex(), "decoded": dec, "canonical": st.as_dict() if st else None})
                else:
                    if changed or args.all:
                        print(f"raw: {raw.hex()}")
                        print(f"  packet={dec['packet']} L={dec['buttons_l']} H={dec['buttons_h']} "
                              f"bits={dec['buttons']} unknown={dec['unknown_bits']}")
                        print(f"  canonical={button_names(buttons)}")
                    print(f"  sticks L={dec['lstick']} R={dec['rstick']} trig L={dec['trigger_l']} R={dec['trigger_r']}"
                          + (f" lpad={dec['lpad']} rpad={dec['rpad']} gyro={dec['gyro']} accel={dec['accel']}"
                             if args.sensors else ""), flush=True)
                last_summary = now
                last_buttons = buttons
        print(f"probe done: {count} state reports, {other} other packets, heartbeats={src.heartbeats}",
              file=sys.stderr, flush=True)
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"probe failed: {exc}", file=sys.stderr)
        return 1
    finally:
        src.close()


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
